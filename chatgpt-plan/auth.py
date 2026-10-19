"""MaiBot 独立的官方 ChatGPT OAuth 注册、凭据存储与刷新。"""

from contextlib import contextmanager
from pathlib import Path
from typing import Any, Dict, Iterator, Optional
from urllib.parse import urlencode

import base64
import fcntl
import hashlib
import json
import os
import re
import secrets
import time
import uuid

import httpx
import jwt

ISSUER = "https://auth.openai.com"
RESOURCE = "https://api.openai.com/v1"
TOKEN_URL = ISSUER + "/api/accounts/oauth/token"
SCOPES = "openid profile email offline_access resource.invoke chatgpt.tokens.use.direct"
STORE = Path.home() / ".config/maibot-chatgpt-plan/accounts.json"
# 允许认证服务器与本机存在少量时间偏差，仍校验签名、身份及有效期。
IDENTITY_CLOCK_LEEWAY_SECONDS = 30


class AuthError(RuntimeError):
    """只携带可公开的错误类别，不包含凭据或完整服务端响应。"""


def atomic_save(path: Path, value: Dict[str, Any]) -> None:
    """以仅所有者可读的权限原子替换凭据，避免刷新时留下半份文件。"""
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    os.chmod(path.parent, 0o700)
    temporary = path.with_name(path.name + "." + secrets.token_hex(8) + ".tmp")
    fd = os.open(temporary, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(value, handle, ensure_ascii=False)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


class AccountStore:
    """跨进程串行刷新；不同账号各自保留客户端、身份与令牌。"""

    def __init__(self, path: Path = STORE):
        self.path = path

    @contextmanager
    def locked(self) -> Iterator[Dict[str, Any]]:
        self.path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        os.chmod(self.path.parent, 0o700)
        fd = os.open(str(self.path) + ".lock", os.O_CREAT | os.O_RDWR, 0o600)
        with os.fdopen(fd, "w") as handle:
            fcntl.flock(handle, fcntl.LOCK_EX)
            data = json.loads(self.path.read_text()) if self.path.exists() else {
                "host_id": "urn:uuid:" + str(uuid.uuid4()), "active": None, "accounts": {},
            }
            if not self.path.exists():
                atomic_save(self.path, data)
            yield data

    def public_status(self) -> Dict[str, Any]:
        with self.locked() as data:
            accounts = [{"key": key, "email": value.get("email", ""),
                         "signed_in": bool(value.get("access_token")),
                         "active": key == data["active"]}
                        for key, value in data["accounts"].items()]
            return {"authenticated": any(a["active"] and a["signed_in"] for a in accounts),
                    "accounts": accounts}

    def begin(self, callback: str, account: Optional[str] = None) -> Dict[str, Any]:
        with self.locked() as data:
            saved = data["accounts"].get(account) if account else None
            if account and saved is None:
                raise AuthError("未知账号")
            pending = {"state": secrets.token_urlsafe(32), "nonce": secrets.token_urlsafe(32),
                       "verifier": secrets.token_urlsafe(64), "callback": callback,
                       "created": time.time(), "account": account,
                       "client_id": saved["client_id"] if saved else "dynamic_agent_client"}
            challenge = base64.urlsafe_b64encode(hashlib.sha256(pending["verifier"].encode()).digest()).decode().rstrip("=")
            query = {"client_id": pending["client_id"], "ext_agent_host_id": data["host_id"],
                     "response_type": "code", "redirect_uri": callback, "scope": SCOPES,
                     "resource": RESOURCE, "state": pending["state"], "nonce": pending["nonce"],
                     "code_challenge_method": "S256", "code_challenge": challenge}
            if saved:
                if saved.get("id_token"):
                    query["id_token_hint"] = saved["id_token"]
                if saved.get("email"):
                    query["login_hint"] = saved["email"]
            else:
                query["agent_name_hint"] = "MaiBot QQbot"
            pending["url"] = ISSUER + "/api/accounts/authorize?" + urlencode(query)
            return pending

    def complete(self, pending: Dict[str, Any], query: Dict[str, str]) -> None:
        if time.time() - pending["created"] > 600:
            raise AuthError("登录已超时，请重新开始")
        if not secrets.compare_digest(query.get("state", ""), pending["state"]):
            raise AuthError("登录状态校验失败")
        if query.get("error"):
            raise AuthError("登录未授权，请重新开始")
        client_id = query.get("client_id", pending["client_id"])
        if client_id == "dynamic_agent_client" or not client_id or not query.get("code"):
            raise AuthError("注册回调不完整")
        if pending["account"] and client_id != pending["client_id"]:
            raise AuthError("返回的客户端与所选账号不一致")
        with self.locked() as data:
            tokens = exchange({"grant_type": "authorization_code", "client_id": client_id,
                               "code": query["code"], "code_verifier": pending["verifier"],
                               "redirect_uri": pending["callback"], "resource": RESOURCE})
            claims = validate_identity(tokens.get("id_token", ""), client_id, pending["nonce"])
            check_scopes(tokens)
            saved = data["accounts"].get(pending["account"]) if pending["account"] else None
            if saved and claims["sub"] != saved["subject"]:
                raise AuthError("登录身份与所选账号不一致")
            key = hashlib.sha256((client_id + "\0" + claims["sub"]).encode()).hexdigest()[:24]
            record = {"client_id": client_id, "subject": claims["sub"], "issuer": ISSUER,
                      "email": claims.get("email", ""), "ext_agent_host_id": data["host_id"]}
            update_tokens(record, tokens)
            data["accounts"][key] = record
            data["active"] = key
            atomic_save(self.path, data)

    def token(self) -> str:
        with self.locked() as data:
            record = data["accounts"].get(data["active"])
            if not record or not record.get("access_token"):
                raise AuthError("请先在本机登录页 Continue with ChatGPT")
            if time.time() >= record["expires_at"] - 120:
                if not record.get("refresh_token"):
                    raise AuthError("登录已过期，请重新登录")
                tokens = exchange({"grant_type": "refresh_token", "client_id": record["client_id"],
                                   "refresh_token": record["refresh_token"], "resource": RESOURCE})
                check_scopes(tokens)
                if tokens.get("id_token"):
                    claims = validate_identity(tokens["id_token"], record["client_id"])
                    if claims["sub"] != record["subject"]:
                        raise AuthError("刷新返回了不同的账号身份")
                update_tokens(record, tokens)
                atomic_save(self.path, data)
            return record["access_token"]

    def select(self, key: str) -> None:
        with self.locked() as data:
            if key not in data["accounts"] or not data["accounts"][key].get("access_token"):
                raise AuthError("此账号需要先登录")
            data["active"] = key
            atomic_save(self.path, data)

    def sign_out(self, key: str) -> None:
        with self.locked() as data:
            record = data["accounts"].get(key)
            if record is None:
                raise AuthError("未知账号")
            # 先尝试结束可刷新的会话，保留客户端与身份映射供下次登录复用。
            if record.get("refresh_token"):
                try:
                    with httpx.Client(timeout=30) as client:
                        client.post(ISSUER + "/api/accounts/oauth/revoke", data={
                            "client_id": record["client_id"], "token": record["refresh_token"],
                            "token_type_hint": "refresh_token"})
                except httpx.HTTPError:
                    pass
            for field in ["access_token", "refresh_token", "id_token"]:
                record.pop(field, None)
            if data["active"] == key:
                data["active"] = None
            atomic_save(self.path, data)


def exchange(form: Dict[str, str]) -> Dict[str, Any]:
    with httpx.Client(timeout=30) as client:
        response = client.post(TOKEN_URL, data=form)
    if response.status_code != 200:
        try:
            code = str(response.json().get("error", ""))
        except ValueError:
            code = ""
        safe_code = code if re.fullmatch(r"[a-zA-Z0-9_]{1,80}", code) else "oauth_error"
        raise AuthError(f"OAuth HTTP {response.status_code}: {safe_code}")
    return response.json()


def validate_identity(token: str, client_id: str, nonce: Optional[str] = None) -> Dict[str, Any]:
    """验证签名、身份及时间声明，容忍30秒时钟偏差；登录额外核对 nonce。"""
    header = jwt.get_unverified_header(token)
    if header.get("alg") != "RS256":
        raise AuthError("不支持的身份令牌签名算法")
    with httpx.Client(timeout=30) as client:
        response = client.get(ISSUER + "/.well-known/jwks.json")
        response.raise_for_status()
        keys = response.json()["keys"]
    key = next((k for k in keys if k.get("kid") == header.get("kid")), None)
    if key is None:
        raise AuthError("身份签名密钥不存在")
    claims = jwt.decode(token, jwt.PyJWK.from_dict(key).key, algorithms=["RS256"],
                        audience=client_id, issuer=ISSUER, leeway=IDENTITY_CLOCK_LEEWAY_SECONDS,
                        options={"require": ["exp", "iat", "sub", "aud", "iss"]})
    if nonce is not None and not secrets.compare_digest(claims.get("nonce", ""), nonce):
        raise AuthError("登录 nonce 校验失败")
    return claims


def check_scopes(tokens: Dict[str, Any]) -> None:
    if not {"chatgpt.tokens.use.direct", "resource.invoke", "offline_access"}.issubset(
        set(tokens.get("scope", "").split())
    ):
        raise AuthError("尚未授权 ChatGPT plan 推理与刷新权限")
    if not tokens.get("access_token") or not tokens.get("refresh_token"):
        raise AuthError("OAuth 没有返回完整凭据")


def update_tokens(record: Dict[str, Any], tokens: Dict[str, Any]) -> None:
    for field in ["access_token", "refresh_token", "id_token"]:
        if tokens.get(field):
            record[field] = tokens[field]
    record["scopes"] = tokens["scope"].split()
    record["expires_at"] = time.time() + int(tokens.get("expires_in", 3600))
    record["saved_at"] = time.time()

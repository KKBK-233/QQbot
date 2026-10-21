"""日志与用户可见文本的敏感信息脱敏工具。"""

from typing import Iterable, List, Mapping, Optional
from urllib.parse import urlsplit, urlunsplit

import logging
import os
import re

REDACTED = "[REDACTED]"

_SENSITIVE_QUERY_PATTERN = re.compile(r"(?i)((?:api[_-]?key|apikey|access[_-]?token|token|secret|key)=)[^&\s\"']+")
_LONG_TOKEN_PATTERN = re.compile(r"\b[A-Fa-f0-9]{24,}\b")
_URL_PATTERN = re.compile(r"https?://[^\s\"'<>]+")


def resolve_api_key(
    explicit_key: str,
    env_name: str,
    environ: Optional[Mapping[str, str]] = None,
) -> str:
    """优先读取配置中的 key，缺失时回退到环境变量。

    Args:
        explicit_key: 配置文件中显式填写的 key。
        env_name: 环境变量名称。
        environ: 环境变量映射；留空时读取 ``os.environ``。

    Returns:
        str: 解析到的 key；均未配置时返回空字符串。
    """

    configured = str(explicit_key or "").strip()
    if configured:
        return configured
    name = str(env_name or "").strip()
    if not name:
        return ""
    source = environ if environ is not None else os.environ
    return str(source.get(name, "") or "").strip()


def redact_text(text: object, secrets: Iterable[str] = ()) -> str:
    """移除文本中的已知密钥和常见令牌参数。

    该函数只用于日志与错误摘要；不会修改引擎返回的原始数据。
    """

    result = str(text or "")
    for secret in secrets:
        normalized = str(secret or "")
        if len(normalized) >= 4:
            result = result.replace(normalized, REDACTED)
    result = _SENSITIVE_QUERY_PATTERN.sub(r"\1" + REDACTED, result)
    result = _LONG_TOKEN_PATTERN.sub(REDACTED, result)
    return result


def mask_url(url: str) -> str:
    """只保留 URL 的协议、主机和路径，隐藏查询参数与片段。"""

    text = str(url or "").strip()
    if not text:
        return ""
    try:
        parsed = urlsplit(text)
    except ValueError:
        return REDACTED
    if not parsed.scheme or not parsed.netloc:
        return REDACTED
    try:
        host = parsed.hostname or ""
        if ":" in host and not host.startswith("["):
            host = f"[{host}]"
        port = f":{parsed.port}" if parsed.port else ""
    except ValueError:
        return REDACTED
    path = parsed.path or "/"
    base = urlunsplit((parsed.scheme, f"{host}{port}", path, "", ""))
    return f"{base}?[REDACTED]" if parsed.query else base


def safe_reason(message: object, secrets: Iterable[str] = (), limit: int = 160) -> str:
    """生成单行、限长、脱敏的错误原因。"""

    text = redact_text(message, secrets)
    text = " ".join(text.split())
    if len(text) > limit:
        text = text[: limit - 1] + "…"
    return text


class SecretRedactingFilter(logging.Filter):
    """第三方 HTTP 客户端日志的脱敏过滤器。

    ``httpx`` 会在 INFO 级别输出完整请求 URL，其中可能带有 SauceNAO 的
    ``api_key`` 查询参数。该过滤器同时替换已知密钥、敏感查询参数和 URL
    查询串，避免凭据进入日志。
    """

    def __init__(self, secrets: Iterable[str] = ()) -> None:
        """保存需要脱敏的密钥列表。"""

        super().__init__()
        self._secrets: List[str] = [str(secret) for secret in secrets if str(secret)]

    def filter(self, record: logging.LogRecord) -> bool:
        """就地改写日志消息，始终允许记录继续输出。"""

        try:
            message = record.getMessage()
        except Exception:
            return True
        redacted = redact_text(message, self._secrets)
        redacted = _URL_PATTERN.sub(lambda match: mask_url(match.group(0)), redacted)
        if redacted != message:
            record.msg = redacted
            record.args = ()
        return True

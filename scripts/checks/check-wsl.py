"""检查本机管理鉴权与 QQ 接入状态，不打印任何登录凭据。"""

from pathlib import Path
import json
import urllib.error
import urllib.request

ROOT = Path("/home/qqbot/QQbot")


def request_json(url: str, payload: dict | None = None, token: str = "") -> dict:
    """请求本机接口，凭据只放在请求体或 Authorization 头中。"""
    headers = {"Content-Type": "application/json"}
    if token:
        headers["Authorization"] = "Bearer " + token
    req = urllib.request.Request(url, headers=headers,
                                 data=json.dumps(payload).encode() if payload is not None else None)
    with urllib.request.urlopen(req, timeout=10) as response:
        return json.load(response)


auth = json.loads((ROOT / "MaiBot/data/webui.json").read_text())
maibot = request_json("http://127.0.0.1:8001/api/webui/auth/verify", {"token": auth["access_token"]})
assert maibot.get("valid"), "麦麦管理鉴权失败"
print("麦麦管理：鉴权通过")
password = (ROOT / "run/管理登录.txt").read_text().split("登录密码：", 1)[1].splitlines()[0]
snow = request_json("http://127.0.0.1:5099/api/login", {"password": password})
assert snow.get("success"), "SnowLuma 管理鉴权失败"
print("SnowLuma 管理：鉴权通过")
try:
    info = request_json("http://127.0.0.1:3000/get_login_info", {},
                        (ROOT / "run/onebot.token").read_text().strip())
    if info.get("status") == "ok" and info.get("data", {}).get("user_id"):
        print("QQ 接入：已登录，账号 ID 已由适配器自动读取")
    else:
        print("QQ 接入：等待账号登录")
except (urllib.error.URLError, TimeoutError):
    print("QQ 接入：等待扫码登录后建立 OneBot 服务")

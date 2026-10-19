"""准备 WSL 中的本地模型配置和 SnowLuma 连接凭据，不输出密钥。"""

from datetime import datetime, timezone
from pathlib import Path
import hashlib
import json
import os
import secrets
import sys

ROOT = Path("/home/qqbot/QQbot")
MAIBOT = ROOT / "MaiBot"
sys.path.insert(0, str(MAIBOT))
os.chdir(MAIBOT)
os.umask(0o077)

import tomlkit
from src.config.config import (
    CONFIG_VERSION,
    MODEL_CONFIG_VERSION,
    Config,
    ModelConfig,
    generate_new_config_file,
)

# 从当前正式版的配置模型生成完整配置，保留未定制项的默认值。
config_dir = MAIBOT / "config"
config_dir.mkdir(exist_ok=True)
bot_path = config_dir / "bot_config.toml"
model_path = config_dir / "model_config.toml"
if not bot_path.exists():
    generate_new_config_file(Config, bot_path, CONFIG_VERSION)
if not model_path.exists():
    generate_new_config_file(ModelConfig, model_path, MODEL_CONFIG_VERSION, True)

key_path = Path("/home/qqbot/.config/qqbot/deepseek.key")
key = key_path.read_text().strip()
model_doc = tomlkit.parse(model_path.read_text())
model_doc["api_providers"] = [{
    "name": "DeepSeek", "base_url": "https://api.deepseek.com", "api_key": key,
    "client_type": "openai", "auth_type": "bearer", "max_retry": 2,
    "timeout": 120, "retry_interval": 4,
}]
model_doc["models"] = [{
    "name": "deepseek-flash", "model_identifier": "deepseek-flash",
    "api_provider": "DeepSeek", "visual": True, "send_temperature": True,
    "extra_params": {"thinking": {"type": "disabled"}},
}]
for name, task in model_doc["model_task_config"].items():
    if isinstance(task, dict):
        task["model_list"] = [] if name in {"voice", "embedding", "image_embedding"} else ["deepseek-flash"]
model_path.write_text(tomlkit.dumps(model_doc))
model_path.chmod(0o600)
bot_doc = tomlkit.parse(bot_path.read_text())
bot_doc["bot"]["platform"] = "qq"
bot_doc["webui"]["host"] = ["127.0.0.1"]
bot_doc["telemetry"]["enable"] = False
# 独立识图失败时仍能处理聊天，防止异常图片污染整段回复请求。
bot_doc["visual"]["planner_mode"] = "text"
bot_doc["visual"]["replyer_mode"] = "text"
# 避免分句数量超限时把真实回答替换成随机默认回复。
bot_doc["response_splitter"]["enable_overflow_return_all"] = True
bot_path.write_text(tomlkit.dumps(bot_doc))

# 默认开放私聊；群聊需要在管理界面选定具体群后放行。
policy_path = config_dir / "adapter_policy.toml"
if not policy_path.exists():
    policy_path.write_text('[defaults.group]\ndefault_action = "block"\n\n[defaults.private]\ndefault_action = "allow"\n')

# 两端共享随机 OneBot 令牌，仅允许本机连接。
snow_config = ROOT / "SnowLuma" / "config"
snow_config.mkdir(exist_ok=True)
token_path = ROOT / "run" / "onebot.token"
token = token_path.read_text().strip() if token_path.exists() else secrets.token_urlsafe(32)
token_path.write_text(token)
base = {"enabled": True, "host": "127.0.0.1", "path": "/", "accessToken": token,
        "messageFormat": "array", "reportSelfMessage": False}
onebot = {"networks": {
    "httpServers": [{**base, "name": "http-default", "port": 3000}],
    "wsServers": [{**base, "name": "ws-default", "port": 3001, "role": "Universal"}],
    "httpClients": [], "wsClients": [],
}, "statusCommand": {"enabled": False}, "historySync": {"enabled": False}}
(snow_config / "onebot.json").write_text(json.dumps(onebot, indent=2))
(snow_config / "runtime.json").write_text(json.dumps({
    "webuiHost": "127.0.0.1", "webuiPort": 5099, "hookAutoLoad": True,
}))
adapter_path = MAIBOT / "plugins" / "MaiBot-SnowLuma-Adapter" / "config.toml"
adapter_doc = tomlkit.parse(adapter_path.read_text())
adapter_doc["client"]["token"] = token
adapter_path.write_text(tomlkit.dumps(adapter_doc))
adapter_path.chmod(0o600)

# 保存本机管理登录凭据，避免程序反复生成初始密码并写进启动日志。
login_path = ROOT / "run" / "管理登录.txt"
if not (snow_config / "webui.json").exists():
    password = "Qq!" + secrets.token_urlsafe(18)
    salt = secrets.token_bytes(16)
    digest = hashlib.scrypt(password.encode(), salt=salt, n=16384, r=8, p=1, dklen=64)
    now = datetime.now(timezone.utc).isoformat()
    (snow_config / "webui.json").write_text(json.dumps({
        "passwordHash": digest.hex(), "passwordSalt": salt.hex(),
        "mustChangePassword": False, "generatedAt": now, "updatedAt": now,
    }))
    login_path.write_text("SnowLuma：http://localhost:5099\n登录密码：" + password + "\n")
    login_path.chmod(0o600)

webui_path = MAIBOT / "data" / "webui.json"
if webui_path.exists():
    webui_auth = json.loads(webui_path.read_text())
else:
    webui_path.parent.mkdir(exist_ok=True)
    webui_auth = {}
if webui_auth.get("token_source") != "configured":
    webui_auth["access_token"] = "Mai!" + secrets.token_urlsafe(24)
webui_auth["token_source"] = "configured"
webui_auth["first_setup_completed"] = True
webui_path.write_text(json.dumps(webui_auth, indent=2))
webui_path.chmod(0o600)
login_text = login_path.read_text().split("\n麦麦：")[0]
login_path.write_text(login_text + "\n麦麦：http://localhost:8001\n登录 Token：" + webui_auth["access_token"] + "\n")

print("本地配置完成；密钥及连接令牌未输出。")

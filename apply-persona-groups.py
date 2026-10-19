"""应用伊蕾娜人设和指定群白名单，仅修改本地配置，不发送消息。"""

from pathlib import Path
import json
import os

import tomlkit

ROOT = Path("/home/qqbot/QQbot")
os.umask(0o077)
persona = json.loads(Path("/mnt/d/code/web/QQbot/persona-elaina.json").read_text())
config_path = ROOT / "MaiBot/config/bot_config.toml"
config = tomlkit.parse(config_path.read_text())
config["bot"]["nickname"] = persona["nickname"]
config["bot"]["alias_names"] = persona["alias_names"]
for field in ["personality", "behavior_style", "reply_style", "multiple_reply_style", "multiple_probability"]:
    config["personality"][field] = persona[field]

# 默认规则阻止其他群，保留后续授权开放的群，避免重设人设时缩回名单。
groups = json.loads((ROOT / "run/target-groups.json").read_text())
expected_ids = {894179464, 1022382380}
assert {int(group["group_id"]) for group in groups} == expected_ids, "群号解析与预期不符"
policy_path = ROOT / "MaiBot/config/adapter_policy.toml"
policy = tomlkit.parse(policy_path.read_text())
policy["defaults"]["group"]["default_action"] = "block"
allowed_ids = set(policy["defaults"]["group"].get("allow_ids", []))
policy["defaults"]["group"]["allow_ids"] = sorted(allowed_ids | {str(value) for value in expected_ids})
policy_path.write_text(tomlkit.dumps(policy))
config_path.write_text(tomlkit.dumps(config))
print("已配置伊蕾娜人设，放行初始指定群并保留后续授权群。")

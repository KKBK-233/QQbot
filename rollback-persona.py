"""撤回尚未获审阅确认的伊蕾娜人设，保留群名单和模型配置。"""

from pathlib import Path
import os
import sys

ROOT = Path("/home/qqbot/QQbot/MaiBot")
os.chdir(ROOT)
sys.path.insert(0, str(ROOT))

import tomlkit
from src.config.official_configs import BotConfig, PersonalityConfig

path = ROOT / "config/bot_config.toml"
config = tomlkit.parse(path.read_text())
personality = PersonalityConfig()
bot = BotConfig()
config["bot"]["nickname"] = bot.nickname
config["bot"]["alias_names"] = bot.alias_names
for name in ["personality", "behavior_style", "reply_style", "multiple_reply_style", "multiple_probability"]:
    config["personality"][name] = getattr(personality, name)
path.write_text(tomlkit.dumps(config))
print("伊蕾娜人设已撤回，群白名单与 DeepSeek 配置保留。")

"""保留分句超限的真实回答，避免被随机默认回复替换。"""

from datetime import datetime, timezone
from pathlib import Path

import os
import shutil

import tomlkit

root = Path('/home/qqbot/QQbot')
os.umask(0o077)
path = root / 'MaiBot/config/bot_config.toml'
config = tomlkit.parse(path.read_text())
backup = root / 'run/config-backups' / datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%S%fZ')
backup.mkdir(parents=True)
shutil.copy2(path, backup / path.name)
# 上游已有此开关：分句超限时发完整答案，不伪造“懒得说”等回答。
config['response_splitter']['enable_overflow_return_all'] = True
path.write_text(tomlkit.dumps(config))
print('分句超限改为保留原文；原配置已备份。')

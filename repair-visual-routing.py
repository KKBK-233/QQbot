"""隔离视觉接口失败，避免群聊图片阻断文字回复。"""

from datetime import datetime, timezone
from pathlib import Path
import os
import shutil
import tomlkit

ROOT = Path('/home/qqbot/QQbot')
os.umask(0o077)
path = ROOT / 'MaiBot/config/bot_config.toml'
config = tomlkit.parse(path.read_text())
backup = ROOT / 'run/config-backups' / datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%S%fZ')
backup.mkdir(parents=True)
shutil.copy2(path, backup / path.name)
# 图片仍由独立识图任务处理；规划和回复只使用文字及识图结果。
config['visual']['planner_mode'] = 'text'
config['visual']['replyer_mode'] = 'text'
path.write_text(tomlkit.dumps(config))
print('规划、回复已改为文字及识图结果输入；原配置已备份，独立识图保持启用。')

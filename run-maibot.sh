#!/usr/bin/env bash
set -euo pipefail
# 启动麦麦，首次部署的协议确认使用当前文件哈希。
cd /home/qqbot/QQbot/MaiBot
export TZ=Asia/Shanghai
export EULA_AGREE="$(python3 -c 'import hashlib,pathlib; print(hashlib.md5(pathlib.Path("EULA.md").read_text().encode()).hexdigest())')"
export PRIVACY_AGREE="$(python3 -c 'import hashlib,pathlib; print(hashlib.md5(pathlib.Path("PRIVACY.md").read_text().encode()).hexdigest())')"
exec .venv/bin/python bot.py

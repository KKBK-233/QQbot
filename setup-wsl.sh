#!/usr/bin/env bash
set -euo pipefail
# 安装 Linux QQ 和运行依赖，使用独立普通用户保存登录态及密钥。
SOURCE=/mnt/d/code/web/QQbot
TARGET=/home/qqbot/QQbot
export DEBIAN_FRONTEND=noninteractive
apt-get update
apt-get install -y python3-venv python3-pip libcap2-bin supervisor dbus-x11 fonts-wqy-zenhei "$SOURCE/downloads/QQ_3.2.32_260812_amd64_01.deb"
if ! id qqbot >/dev/null 2>&1; then
    useradd --create-home --shell /bin/bash qqbot
fi
install -d -o qqbot -g qqbot "$TARGET" "$TARGET/logs" "$TARGET/run"
for component in MaiBot SnowLuma; do
    if [[ ! -d "$TARGET/$component" ]]; then
        cp -a "$SOURCE/$component" "$TARGET/"
    fi
done
chown -R qqbot:qqbot "$TARGET"
install -d -m 700 -o qqbot -g qqbot /home/qqbot/.config /home/qqbot/.config/QQ
chmod 700 /home/qqbot "$TARGET"
# 只授予独立 SnowLuma 运行时进程注入权限，不修改系统 ptrace 全局策略。
setcap cap_sys_ptrace=ep "$TARGET/SnowLuma/node"
runuser -u qqbot -- python3 -m venv /home/qqbot/.uv-bootstrap
runuser -u qqbot -- /home/qqbot/.uv-bootstrap/bin/pip install uv
runuser -u qqbot -- bash -c 'cd /home/qqbot/QQbot/MaiBot && /home/qqbot/.uv-bootstrap/bin/uv sync --locked --no-dev && /home/qqbot/.uv-bootstrap/bin/uv pip install "silk-python>=0.2.8"'

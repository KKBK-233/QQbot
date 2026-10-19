#!/usr/bin/env bash
set -euo pipefail
# 重复执行只检查现有进程；WSL 重启后重新拉起整套服务。
umask 077
cd /home/qqbot/QQbot
mkdir -p run/xdg logs
chmod 700 run/xdg
if ! supervisorctl -c supervisord.conf pid >/dev/null 2>&1; then
    supervisord -c supervisord.conf
fi
supervisorctl -c supervisord.conf status

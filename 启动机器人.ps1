# 从 Windows 启动 WSL 中的 QQ 登录窗口和机器人服务。
$ErrorActionPreference = 'Stop'
wsl.exe -d Ubuntu -u root -- python3 /mnt/d/code/web/QQbot/guard-qq-version.py
if ($LASTEXITCODE -ne 0) { throw 'QQ 版本保护检查失败。' }
# WSL 重启后恢复系统 D-Bus，供 QQ 桌面客户端使用。
wsl.exe -d Ubuntu -u root -- bash -c 'mkdir -p /run/dbus; test -S /run/dbus/system_bus_socket || dbus-daemon --system --fork'
if ($LASTEXITCODE -ne 0) { throw 'D-Bus 启动失败。' }
wsl.exe -d Ubuntu -u qqbot -- bash /home/qqbot/QQbot/start-wsl.sh
if ($LASTEXITCODE -ne 0) { throw '启动失败，请检查 WSL 中 logs 目录。' }
Write-Host 'QQ 使用原有用户数据启动；先检查会话是否恢复，仅在客户端要求验证时登录。'
Write-Host '麦麦管理：http://localhost:8001；SnowLuma：http://localhost:5099'

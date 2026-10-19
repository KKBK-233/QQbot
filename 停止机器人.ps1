# 只关闭这套部署的服务，不停止整个 WSL。
wsl.exe -d Ubuntu -u qqbot -- supervisorctl -c /home/qqbot/QQbot/supervisord.conf shutdown

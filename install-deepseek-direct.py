"""仅给WSL Supervisor增加DeepSeek固定目标本机通道，保留其他运行配置。"""
from datetime import datetime, timezone
from pathlib import Path
import configparser
import shutil

source = Path('/mnt/d/code/web/QQbot/supervisord.conf').read_text(encoding='utf-8')
parser = configparser.ConfigParser(interpolation=None)
parser.read_string(source)
section = 'program:deepseek-direct'
block = '[' + section + ']\n' + '\n'.join(key + '=' + value for key, value in parser[section].items()) + '\n'
target = Path('/home/qqbot/QQbot/supervisord.conf')
current = target.read_text()
if '[' + section + ']' not in current:
    stamp = datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')
    shutil.copy2(target, target.with_name(target.name + '.before-windows-direct-' + stamp))
    target.write_text(current.rstrip() + '\n\n' + block)
print('DeepSeek本机通道配置已安装；其他服务配置保持')

"""仅部署本次原图状态修复，核对旧源码并保留私有备份，不重启 QQ。"""
from datetime import datetime, timezone
from pathlib import Path
import shutil
import subprocess

source = Path('/mnt/d/code/web/QQbot/image-tool/maibot-plugin')
target = Path('/home/qqbot/QQbot/MaiBot/plugins/qqbot-image')
names = ['plugin.py', 'reply_facts.py', 'media_facts.py']
# 修改前校验部署版本与当前提交一致，避免覆盖运行目录内的独立改动。
for name in names:
    deployed = target / name
    old = subprocess.check_output(['git', '-c', 'safe.directory=' + str(source.parent.parent),
                                   '-C', str(source.parent.parent), 'show', 'HEAD:image-tool/maibot-plugin/' + name])
    assert deployed.read_bytes().replace(b'\r\n', b'\n') in {
        old.replace(b'\r\n', b'\n'), (source / name).read_bytes().replace(b'\r\n', b'\n')}, name + '部署源码存在独立修改'
backup = target.parent.parent / 'data/image-media-fix' / datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')
backup.mkdir(parents=True, mode=0o700)
backup.chmod(0o700)
for name in names:
    if (target / name).exists():
        shutil.copy2(target / name, backup / name)
    shutil.copy2(source / name, target / name)
    assert (target / name).read_bytes() == (source / name).read_bytes()
print('原图状态修复已部署，三个文件逐项核对通过。')

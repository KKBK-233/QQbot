"""管理员一次性安装固定启动器，不给机器人通用sudo、Shell或配置权限。"""

import importlib.util
import os
from pathlib import Path
import pwd
import shutil
import subprocess
import tempfile


def main():
    """部署root所有、普通账号不可修改的启动器和worker。"""
    if os.geteuid() != 0:
        raise SystemExit('请以WSL root运行此安装脚本')
    source = Path(__file__).resolve().parent
    try:
        pwd.getpwnam('qqbot-sandbox')
    except KeyError:
        subprocess.run(['/usr/sbin/useradd', '--system', '--no-create-home', '--home-dir', '/nonexistent',
                        '--shell', '/usr/sbin/nologin', 'qqbot-sandbox'], check=True)
    paths = [(source / 'launcher.py', Path('/usr/local/libexec/qqbot-python-launch'), 0o755),
             (source / 'kill.py', Path('/usr/local/libexec/qqbot-python-kill'), 0o755),
             (source / 'worker.py', Path('/usr/local/share/qqbot-python-sandbox/worker.py'), 0o644)]
    for original, target, mode in paths:
        target.parent.mkdir(parents=True, exist_ok=True)
        if target.is_symlink():
            raise RuntimeError('拒绝覆盖软链接')
        with tempfile.NamedTemporaryFile(dir=target.parent, delete=False) as temporary:
            temporary.write(original.read_bytes())
            temporary_path = Path(temporary.name)
        temporary_path.chmod(mode)
        os.chown(temporary_path, 0, 0)
        os.replace(temporary_path, target)
    # 空字符串精确要求无参数；不能利用此规则启动任意命令或选择挂载路径。
    rule = ('qqbot ALL=(root) NOPASSWD: /usr/local/libexec/qqbot-python-launch "", '
            '/usr/local/libexec/qqbot-python-kill ""\n')
    with tempfile.NamedTemporaryFile(mode='w', delete=False) as temporary:
        temporary.write(rule)
        candidate = Path(temporary.name)
    try:
        subprocess.run(['/usr/sbin/visudo', '-c', '-f', str(candidate)], check=True, capture_output=True)
        target = Path('/etc/sudoers.d/qqbot-python-sandbox')
        shutil.copyfile(candidate, target)
        target.chmod(0o440)
        os.chown(target, 0, 0)
    finally:
        candidate.unlink()
    spec = importlib.util.spec_from_file_location('sandbox_limits', source / 'launcher.py')
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    module.prepare()
    print('固定cgroup限额和无参数启动器已安装；QQ/SnowLuma未操作。')


if __name__ == '__main__':
    main()

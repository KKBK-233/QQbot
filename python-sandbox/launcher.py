#!/usr/bin/python3 -I
"""固定的特权启动入口：仅建立限额、降权，再执行只读沙箱；不解析用户代码。"""

import os
from pathlib import Path
import pwd
import sys

CGROUP = Path('/sys/fs/cgroup/qqbot-python-sandbox')
LIMITS = {'memory.max': '402653184', 'memory.swap.max': '0',
          'memory.oom.group': '1', 'cpu.max': '25000 100000', 'pids.max': '8'}


def prepare():
    """只创建固定cgroup并启用所需控制器，不迁移QQ或其他服务。"""
    root = CGROUP.parent
    needed = {'cpu', 'memory', 'pids'}
    if not needed.issubset(set((root / 'cgroup.controllers').read_text().split())):
        raise RuntimeError('内核缺少必要cgroup控制器')
    missing = needed - set((root / 'cgroup.subtree_control').read_text().split())
    if missing:
        (root / 'cgroup.subtree_control').write_text(' '.join('+' + n for n in sorted(missing)))
    CGROUP.mkdir(exist_ok=True)
    for name, value in LIMITS.items():
        (CGROUP / name).write_text(value)


def main():
    """拒绝任何参数；降为专用无登录用户后才启动解释器。"""
    if os.geteuid() != 0 or len(sys.argv) != 1:
        raise SystemExit('此入口只允许固定无参数调用')
    prepare()
    account = pwd.getpwnam('qqbot-sandbox')
    (CGROUP / 'cgroup.procs').write_text(str(os.getpid()))
    os.chdir('/')
    os.setgroups([])
    os.setresgid(account.pw_gid, account.pw_gid, account.pw_gid)
    os.setresuid(account.pw_uid, account.pw_uid, account.pw_uid)
    worker = '/usr/local/share/qqbot-python-sandbox/worker.py'
    args = ['/usr/bin/prlimit', '--as=268435456:268435456', '--cpu=3:3',
            '--fsize=1048576:1048576', '--nofile=48:48', '--core=0:0', '--',
            '/usr/bin/bwrap', '--unshare-all', '--unshare-user', '--disable-userns',
            '--assert-userns-disabled', '--die-with-parent', '--new-session',
            '--cap-drop', 'ALL', '--clearenv',
            '--ro-bind', '/usr/bin/python3.12', '/usr/bin/python3.12',
            '--ro-bind', '/usr/lib/python3.12', '/usr/lib/python3.12',
            '--ro-bind', '/usr/lib/x86_64-linux-gnu', '/usr/lib/x86_64-linux-gnu',
            '--symlink', 'usr/lib', '/lib', '--symlink', 'usr/lib/x86_64-linux-gnu', '/lib64',
            '--ro-bind', worker, '/worker.py', '--dev', '/dev', '--remount-ro', '/dev',
            '--size', '16777216', '--tmpfs', '/work', '--symlink', 'work', '/tmp',
            '--setenv', 'HOME', '/work', '--setenv', 'LANG', 'C.UTF-8',
            '--chdir', '/work', '--remount-ro', '/',
            '/usr/bin/python3.12', '-I', '-S', '-B', '-X', 'utf8', '/worker.py']
    os.execve(args[0], args, {})


if __name__ == '__main__':
    main()

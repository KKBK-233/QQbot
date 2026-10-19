#!/usr/bin/python3 -I
"""只终止固定沙箱cgroup，不接受PID、路径或任何参数。"""

import os
from pathlib import Path
import sys
import time

if os.geteuid() != 0 or len(sys.argv) != 1:
    raise SystemExit('只允许固定无参数调用')
target = Path('/sys/fs/cgroup/qqbot-python-sandbox/cgroup.kill')
if target.exists():
    target.write_text('1')
    # 大量目录项销毁可能晚于SIGKILL交付；必须等内核确认cgroup已空。
    deadline = time.monotonic() + 3
    while (target.parent / 'cgroup.procs').read_text().strip():
        if time.monotonic() >= deadline:
            raise SystemExit('沙箱清理未完成')
        time.sleep(0.02)

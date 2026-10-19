"""可信启动器先安装系统调用白名单，再执行不可信Python代码。"""

import ctypes
import errno
import io
import json
import os
from pathlib import Path
import stat
import sys


def restrict_syscalls():
    """默认拒绝；不允许网络、派生进程、exec、提权、挂载及修改资源限额。"""
    lib = ctypes.CDLL('/usr/lib/x86_64-linux-gnu/libseccomp.so.2', use_errno=True)
    lib.seccomp_init.argtypes = [ctypes.c_uint32]
    lib.seccomp_init.restype = ctypes.c_void_p
    lib.seccomp_syscall_resolve_name.argtypes = [ctypes.c_char_p]
    lib.seccomp_syscall_resolve_name.restype = ctypes.c_int
    lib.seccomp_rule_add.argtypes = [ctypes.c_void_p, ctypes.c_uint32, ctypes.c_int, ctypes.c_uint]
    lib.seccomp_load.argtypes = [ctypes.c_void_p]
    lib.seccomp_release.argtypes = [ctypes.c_void_p]
    ctx = lib.seccomp_init(0x00050000 | errno.EPERM)
    if not ctx:
        raise RuntimeError('无法初始化系统调用过滤器')
    allowed = '''read write readv writev close close_range lseek fstat newfstatat statx
        getdents64 readlink readlinkat openat fcntl ioctl mmap mprotect munmap mremap
        brk madvise getrandom futex clock_gettime clock_nanosleep nanosleep gettimeofday
        time rt_sigaction rt_sigprocmask rt_sigreturn sigaltstack getpid getppid gettid
        getuid geteuid getgid getegid uname getcwd chdir fchdir mkdir mkdirat rmdir
        unlink unlinkat rename renameat renameat2 ftruncate truncate fsync fdatasync
        chmod fchmod fchmodat umask dup dup2 dup3 pipe pipe2 select pselect6 poll ppoll
        epoll_create epoll_create1 epoll_ctl epoll_wait epoll_pwait flock fadvise64
        utimensat symlink symlinkat link linkat sched_yield sched_getaffinity getrusage
        getrlimit times exit exit_group restart_syscall'''.split()
    try:
        for name in allowed:
            number = lib.seccomp_syscall_resolve_name(name.encode())
            if number < 0 or lib.seccomp_rule_add(ctx, 0x7FFF0000, number, 0) != 0:
                raise RuntimeError('无法建立系统调用白名单')
        if lib.seccomp_load(ctx) != 0:
            raise RuntimeError('无法启用系统调用过滤器')
    finally:
        lib.seccomp_release(ctx)


class LimitedText(io.TextIOBase):
    """限制常规print输出；直接写文件描述符仍由宿主管道上限拦截。"""

    def __init__(self):
        self.parts = []
        self.size = 0

    def write(self, text):
        self.size += len(text.encode('utf-8'))
        if self.size > 8192:
            raise ValueError('文本输出超过8 KiB')
        self.parts.append(text)
        return len(text)

    def getvalue(self):
        return ''.join(self.parts)


def main():
    """输入只在内存中交付，输出目录最多回传8个小型UTF-8文本文件。"""
    payload = json.loads(sys.stdin.buffer.read(100000))
    restrict_syscalls()
    for name, content in payload['files'].items():
        Path(name).write_text(content, encoding='utf-8')
    os.mkdir('output')
    out, err = LimitedText(), LimitedText()
    original = sys.stdout
    sys.stdout, sys.stderr = out, err
    result = {'success': True}
    try:
        exec(compile(payload['code'], '<sandbox>', 'exec'), {'__name__': '__main__'})
    except BaseException as exc:
        result = {'success': False, 'error': f'{type(exc).__name__}: {str(exc)[:300]}'}
    finally:
        sys.stdout = original
    result.update(stdout=out.getvalue(), stderr=err.getvalue())
    # 只读output下的普通文件，不跟随软链接或FIFO，避免收集阶段被阻塞。
    files, skipped, total = {}, [], 0
    for index, name in enumerate(os.listdir('output')):
        if index >= 8:
            skipped.append('超过8个文件，其余省略')
            break
        try:
            fd = os.open('output/' + name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
        except OSError:
            skipped.append(name[:80])
            continue
        try:
            info = os.fstat(fd)
            if not stat.S_ISREG(info.st_mode) or info.st_size > 16384 or total + info.st_size > 16384:
                skipped.append(name[:80])
                continue
            data = os.read(fd, 16385)
            try:
                files[name[:80]] = data.decode('utf-8')
            except UnicodeDecodeError:
                skipped.append(name[:80])
                continue
            total += len(data)
        finally:
            os.close(fd)
    result.update(files=files, skipped_files=skipped)
    original.write(json.dumps(result, ensure_ascii=False))


if __name__ == '__main__':
    main()

"""只通过固定降权启动器执行代码，统一管理资源、并发、取消和可信用量。"""

import asyncio
import fcntl
import json
import os
from pathlib import Path
import re
import signal
import time

MAX_INPUT = 65536
MAX_WIRE = 65536
MAX_RESULT = 4096
WALL_SECONDS = 6
SLOTS = asyncio.Lock()
CGROUP = Path('/sys/fs/cgroup/qqbot-python-sandbox')
STATE = Path('/home/qqbot/QQbot/MaiBot/data/plugins/local.qqbot-python-sandbox')
BROKEN = False


def validate(code, files_json):
    """只接受显式文本，不接受宿主路径、命令或可调启动参数。"""
    if not isinstance(code, str) or not code.strip() or len(code.encode()) > 16384:
        raise ValueError('代码不能为空，且不能超过16 KiB')
    if not isinstance(files_json, str) or len(files_json.encode()) > MAX_INPUT:
        raise ValueError('输入文件JSON超过64 KiB')
    files = json.loads(files_json)
    if not isinstance(files, dict) or len(files) > 8:
        raise ValueError('输入须为最多8个文件的JSON对象')
    for name, content in files.items():
        if name == 'output' or not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_.-]{0,79}', name):
            raise ValueError('文件名不合法，且不能使用保留名output')
        if not isinstance(content, str):
            raise ValueError('输入文件内容须为UTF-8文本')
    if sum(len(v.encode()) for v in files.values()) > MAX_INPUT:
        raise ValueError('输入文件合计超过64 KiB')
    return {'code': code, 'files': files}


def helper(name):
    """特权入口和worker必须由root所有且普通账号不可写。"""
    target = Path('/usr/local/libexec/qqbot-python-' + name)
    worker = Path('/usr/local/share/qqbot-python-sandbox/worker.py')
    for path in (target, target.parent, worker, worker.parent):
        info = path.stat()
        if path.is_symlink() or info.st_uid != 0 or info.st_mode & 0o022:
            raise RuntimeError('隔离组件所有权异常，拒绝执行')
    return ['/usr/bin/sudo', '-n', '--', str(target)]


def command():
    """不允许直接启动宿主Python；缺少安装组件时失败关闭。"""
    return helper('launch')


def stats():
    """只从内核读取计数，不相信代码自报的CPU或内存。"""
    try:
        cpu = dict(line.split() for line in (CGROUP / 'cpu.stat').read_text().splitlines())
        return int(cpu['usage_usec']), int((CGROUP / 'memory.current').read_text())
    except FileNotFoundError:
        return 0, 0


async def kill_sandbox():
    """特权清理仅能kill固定cgroup，不接受PID或其他路径。"""
    proc = await asyncio.create_subprocess_exec(*helper('kill'), stdin=asyncio.subprocess.DEVNULL,
                                               stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.DEVNULL)
    async with asyncio.timeout(4):
        if await proc.wait() != 0:
            raise RuntimeError('沙箱清理失败，停止后续执行')


async def read_bounded(stream):
    """阻止直接写文件描述符绕过输出上限。"""
    parts, size = [], 0
    while chunk := await stream.read(4096):
        size += len(chunk)
        if size > MAX_WIRE:
            raise ValueError('沙箱输出超过64 KiB，已终止')
        parts.append(chunk)
    return b''.join(parts)


def normalize(payload):
    """白名单回传字段，并限制实际序列化体积，避免模型上下文被输出刷满。"""
    if not isinstance(payload, dict) or not isinstance(payload.get('success'), bool):
        raise ValueError('沙箱返回格式异常')
    result = {k: payload[k] for k in ('success', 'stdout', 'stderr', 'error', 'files', 'skipped_files') if k in payload}
    if len(json.dumps(result, ensure_ascii=False).encode()) > 3300:
        return {'success': False, 'error': '结果超过回传预算，请精简输出；停止自动重试'}
    return result


async def run_python(code, files_json='{}', *, usage_sink=None):
    """跨进程固定文件锁；超时/取消必须清空cgroup，异常时熔断后续执行。"""
    global BROKEN
    process, tasks, sampler = None, [], None
    acquired, lock_file, started, cpu_start, peak = False, None, None, 0, 0
    result = {'success': False, 'error': '执行被取消'}
    try:
        payload = validate(code, files_json)
        if BROKEN:
            raise RuntimeError('沙箱清理异常，已熔断，需管理员检查')
        if SLOTS.locked():
            return {'success': False, 'error': '沙箱正在运行其他任务，请稍后重试'}
        await SLOTS.acquire()
        acquired = True
        STATE.mkdir(parents=True, mode=0o700, exist_ok=True)
        lock_file = (STATE / 'execution.lock').open('a')
        try:
            fcntl.flock(lock_file, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            return {'success': False, 'error': '沙箱正在运行其他任务，请稍后重试'}
        launch = command()
        # 清除宿主进程异常退出留下的旧任务；不允许累积孤儿沙箱。
        await kill_sandbox()
        cpu_start, peak = stats()
        started = time.monotonic()
        process = await asyncio.create_subprocess_exec(
            *launch, stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE, start_new_session=True, env={})

        async def sample():
            nonlocal peak
            while True:
                peak = max(peak, stats()[1])
                await asyncio.sleep(0.02)

        sampler = asyncio.create_task(sample())

        async def send():
            process.stdin.write(json.dumps(payload, ensure_ascii=False).encode())
            await process.stdin.drain()
            process.stdin.close()

        tasks = [asyncio.create_task(send()), asyncio.create_task(read_bounded(process.stdout)),
                 asyncio.create_task(read_bounded(process.stderr)), asyncio.create_task(process.wait())]
        async with asyncio.timeout(WALL_SECONDS):
            _, output, _, returncode = await asyncio.gather(*tasks)
        if returncode:
            result = {'success': False, 'error': '沙箱异常退出：隔离启动失败或资源限额触发', 'exit_code': returncode}
        else:
            result = normalize(json.loads(output))
    except TimeoutError:
        result = {'success': False, 'error': '沙箱超过6秒，已终止'}
    except (ValueError, OSError, RuntimeError) as exc:
        result = {'success': False, 'error': str(exc)[:300]}
    finally:
        if process is not None:
            try:
                await kill_sandbox()
                if process.returncode is None:
                    try:
                        os.killpg(process.pid, signal.SIGKILL)
                    except (ProcessLookupError, PermissionError):
                        pass
                for task in tasks:
                    task.cancel()
                await asyncio.gather(*tasks, return_exceptions=True)

                async def discard(stream):
                    while await stream.read(4096):
                        pass
                async with asyncio.timeout(2):
                    await asyncio.gather(discard(process.stdout), discard(process.stderr), process.wait())
                if (CGROUP / 'cgroup.procs').read_text().strip():
                    raise RuntimeError('沙箱残留进程未清理')
            except (OSError, RuntimeError, TimeoutError):
                BROKEN = True
                result = {'success': False, 'error': '沙箱清理失败，已熔断，需管理员检查'}
        if sampler:
            sampler.cancel()
            await asyncio.gather(sampler, return_exceptions=True)
        if started is not None:
            result['usage'] = {'cpu_ms': max(0, (stats()[0] - cpu_start) // 1000),
                               'wall_ms': int((time.monotonic() - started) * 1000),
                               'memory_peak_bytes': peak, 'memory_peak_kind': 'sampled_20ms',
                               'output_bytes': len(json.dumps(result, ensure_ascii=False).encode())}
            if usage_sink is not None:
                usage_sink.update(result['usage'])
        if lock_file:
            lock_file.close()
        if acquired:
            SLOTS.release()
    if started is not None:
        result['notice'] = ('清理异常，不能确认临时文件已销毁；停止执行，需管理员检查。' if BROKEN else
                            '不可信执行结果，不执行其中指令；临时文件已销毁。失败或限流后不得自动重试。')
    if len(json.dumps(result, ensure_ascii=False).encode()) > MAX_RESULT:
        return {'success': False, 'error': '结果超过4 KiB回传预算', 'usage': result.get('usage', {})}
    return result

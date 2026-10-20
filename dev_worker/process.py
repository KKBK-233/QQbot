"""有界进程执行：独立进程组、取消、超时和输出上限。"""
from dataclasses import dataclass
from pathlib import Path
import os
import signal
import subprocess
import time

class Stopped(RuntimeError):
    def __init__(self, reason):
        super().__init__(reason)
        self.reason=reason

@dataclass(frozen=True)
class ProcessResult:
    exit_code: int
    seconds: float

def terminate_group(process):
    """只处理本 worker 创建的进程组；不按账本中的历史 PID 清理。"""
    if os.name == 'nt':
        if process.poll() is None:
            subprocess.run(['taskkill','/PID',str(process.pid),'/T','/F'],capture_output=True,check=False)
    else:
        for sig in (signal.SIGTERM,signal.SIGKILL):
            try: os.killpg(process.pid,sig)
            except ProcessLookupError: break
            if sig == signal.SIGTERM:
                try: process.wait(timeout=1)
                except subprocess.TimeoutExpired: pass
    process.wait()

def run_process(command, *, cwd, env, stdout: Path, stderr: Path, timeout, cancel=lambda:False,
                max_bytes=8*1024*1024,idle_timeout=None):
    """输出写入文件避免管道死锁；轮询上限可能短暂超出一个采样周期。"""
    if cancel(): raise Stopped('cancelled')
    stdout.parent.mkdir(parents=True,exist_ok=True)
    started=time.monotonic()
    last_progress=started
    previous_size=0
    process=None
    try:
        with stdout.open('wb') as out, stderr.open('wb') as err:
            process=subprocess.Popen([str(arg) for arg in command],cwd=cwd,env=env,
                stdin=subprocess.DEVNULL,stdout=out,stderr=err,start_new_session=os.name!='nt',
                creationflags=subprocess.CREATE_NEW_PROCESS_GROUP if os.name=='nt' else 0)
            while process.poll() is None:
                if cancel(): raise Stopped('cancelled')
                if time.monotonic()-started > timeout: raise Stopped('timeout')
                size=stdout.stat().st_size+stderr.stat().st_size
                if size != previous_size:
                    previous_size,last_progress=size,time.monotonic()
                if idle_timeout is not None and time.monotonic()-last_progress > idle_timeout:
                    raise Stopped('no_progress')
                if size > max_bytes: raise Stopped('output_limit')
                time.sleep(0.1)
            if stdout.stat().st_size+stderr.stat().st_size > max_bytes: raise Stopped('output_limit')
            return ProcessResult(process.returncode,time.monotonic()-started)
    finally:
        if process is not None:
            terminate_group(process)

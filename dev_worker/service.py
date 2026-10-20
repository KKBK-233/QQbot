"""独立常驻开发服务：仅本机 Unix socket，API 不等待长时间开发任务。"""
from pathlib import Path
import asyncio
import json
import os
import signal
import socket
import stat
import sys
import threading

from .coordinator import Coordinator
from .worker import Worker

MAX_FRAME=131072


async def serve(config, *, ready=None, shutdown=None):
    """同用户宿主经 0600 socket 提交；后台线程仍遵守单 worker 文件锁。"""
    if sys.platform!='linux' or not config.use_sandbox:
        raise RuntimeError('常驻开发服务必须使用 Linux bwrap 隔离')
    stop=threading.Event()
    worker=Worker(config,stop_requested=stop.is_set)
    coordinator=Coordinator(worker)
    directory=config.root/'run'; directory.mkdir(parents=True,exist_ok=True,mode=0o700)
    if directory.is_symlink(): raise ValueError('控制目录不能为符号链接')
    directory.chmod(0o700)
    path=directory/'control.sock'
    if path.exists() or path.is_symlink():
        info=path.lstat()
        if not stat.S_ISSOCK(info.st_mode) or info.st_uid!=os.getuid(): raise ValueError('已有控制路径不属于当前服务')
        with socket.socket(socket.AF_UNIX,socket.SOCK_STREAM) as probe:
            try: probe.connect(str(path))
            except ConnectionRefusedError: path.unlink()
            else: raise RuntimeError('已有开发服务正在运行')
    async def handle(reader,writer):
        try:
            line=await asyncio.wait_for(reader.readline(),10)
            if not line.endswith(b'\n') or len(line)>MAX_FRAME: raise ValueError('请求过大或缺少换行')
            request=json.loads(line)
            result=await asyncio.to_thread(coordinator.dispatch,request)
            response={'success':True,'result':result}
        except Exception as exc:
            response={'success':False,'error':type(exc).__name__,
                      'detail':worker.redactor.text(str(exc))[:512]}
        try:
            writer.write((json.dumps(worker.redactor.value(response),ensure_ascii=False)+'\n').encode())
            await writer.drain()
        finally:
            writer.close()
            await writer.wait_closed()
    server=await asyncio.start_unix_server(handle,path=str(path),limit=MAX_FRAME)
    path.chmod(0o600)
    stopped=shutdown or asyncio.Event()
    loop=asyncio.get_running_loop()
    for sig in (signal.SIGTERM,signal.SIGINT):
        try: loop.add_signal_handler(sig,stopped.set)
        except (RuntimeError,NotImplementedError): pass
    async def execute():
        while not stop.is_set():
            try:
                await asyncio.to_thread(worker.run_once)
                await asyncio.to_thread(worker.store.queue_notifications)
            except BlockingIOError: pass  # 本地 CLI 正在持锁时不抢占。
            except Exception as exc:
                print(json.dumps({'event':'dev_worker_error','error':type(exc).__name__}),flush=True)
                stopped.set()
                raise
            if not stop.is_set(): await asyncio.sleep(1)
    execution=asyncio.create_task(execute())
    if ready is not None: ready.set()
    try:
        async with server: await stopped.wait()
    finally:
        stop.set()
        server.close(); await server.wait_closed()
        await execution
        path.unlink(missing_ok=True)


def main(config):
    asyncio.run(serve(config))

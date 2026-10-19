"""通过 WSL 互操作管道使用 Windows 回环代理，只监听 WSL 本机。"""

import asyncio
import contextlib
import os
import socket
import sys
import threading


def windows_stdio() -> None:
    """透明转发代理字节流，TLS 和 API 密钥不在桥中解密或记录。"""
    import msvcrt

    msvcrt.setmode(sys.stdin.fileno(), os.O_BINARY)
    msvcrt.setmode(sys.stdout.fileno(), os.O_BINARY)
    connection = socket.create_connection(("127.0.0.1", 2080), timeout=10)
    connection.settimeout(None)

    def upload() -> None:
        try:
            while chunk := sys.stdin.buffer.read1(65536):
                connection.sendall(chunk)
        except OSError:
            pass
        finally:
            with contextlib.suppress(OSError):
                connection.shutdown(socket.SHUT_WR)

    threading.Thread(target=upload, daemon=True).start()
    try:
        while chunk := connection.recv(65536):
            sys.stdout.buffer.write(chunk)
            sys.stdout.buffer.flush()
    except (OSError, BrokenPipeError):
        pass
    finally:
        connection.close()


async def handle(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
    """每条本地连接创建独立互操作管道，支持 HTTP CONNECT 长连接。"""
    process = None
    tasks = []
    try:
        process = await asyncio.create_subprocess_exec(
            "/mnt/c/Users/668/AppData/Local/Programs/Python/Python312/python.exe",
            "-u", "D:\\code\\web\\QQbot\\wsl-proxy-bridge.py", "--stdio",
            stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.DEVNULL,
        )

        async def pump(source, destination) -> None:
            while chunk := await source.read(65536):
                destination.write(chunk)
                await destination.drain()

        tasks = [asyncio.create_task(pump(reader, process.stdin)),
                 asyncio.create_task(pump(process.stdout, writer))]
        await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
    finally:
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        if process is not None and process.returncode is None:
            with contextlib.suppress(ProcessLookupError):
                process.terminate()
            await process.wait()
        writer.close()
        with contextlib.suppress(OSError):
            await writer.wait_closed()


async def main() -> None:
    """只在 WSL 回环地址提供机器人专用代理入口。"""
    server = await asyncio.start_server(handle, "127.0.0.1", 2081)
    print("机器人代理桥已启动：WSL 127.0.0.1:2081 → Windows 127.0.0.1:2080", flush=True)
    async with server:
        await server.serve_forever()


if __name__ == "__main__":
    if "--stdio" in sys.argv:
        windows_stdio()
    else:
        asyncio.run(main())

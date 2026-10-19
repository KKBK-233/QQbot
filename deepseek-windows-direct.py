"""DeepSeek专用本机TLS通道：WSL经互操作管道，Windows直接连接官方API。"""
import asyncio
import contextlib
import os
import socket
import sys
import threading

HOST = 'api.deepseek.com'
PORT = 2082


def windows_stdio():
    """只连接固定官方主机，不读取HTTP代理环境变量，不解密TLS或记录内容。"""
    import msvcrt
    msvcrt.setmode(sys.stdin.fileno(), os.O_BINARY)
    msvcrt.setmode(sys.stdout.fileno(), os.O_BINARY)
    try:
        connection = socket.create_connection((HOST, 443), timeout=5)
    except OSError:
        sys.stdout.buffer.write(b'0')
        sys.stdout.buffer.flush()
        return
    connection.settimeout(None)
    sys.stdout.buffer.write(b'1')
    sys.stdout.buffer.flush()

    def upload():
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


async def handle(reader, writer):
    """只允许回环客户端请求官方主机443端口，拒绝通用代理及任意目标。"""
    process = None
    tasks = []
    connected = False
    try:
        header = await asyncio.wait_for(reader.readuntil(b'\r\n\r\n'), 5)
        first = header.split(b'\r\n', 1)[0]
        if len(header) > 4096 or first not in (b'CONNECT api.deepseek.com:443 HTTP/1.1', b'CONNECT api.deepseek.com:443 HTTP/1.0'):
            writer.write(b'HTTP/1.1 403 Forbidden\r\nContent-Length: 0\r\n\r\n')
            await writer.drain()
            return
        process = await asyncio.create_subprocess_exec(
            '/mnt/c/Users/668/AppData/Local/Programs/Python/Python312/python.exe',
            '-u', 'D:/code/web/QQbot/deepseek-windows-direct.py', '--stdio',
            stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.DEVNULL)
        if await asyncio.wait_for(process.stdout.readexactly(1), 8) != b'1':
            raise ConnectionError('Windows建连失败')
        writer.write(b'HTTP/1.1 200 Connection Established\r\n\r\n')
        await writer.drain()
        connected = True

        async def pump(source, destination):
            while chunk := await source.read(65536):
                destination.write(chunk)
                await destination.drain()

        tasks = [asyncio.create_task(pump(reader, process.stdin)),
                 asyncio.create_task(pump(process.stdout, writer))]
        await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
    except (OSError, ValueError, asyncio.IncompleteReadError, asyncio.LimitOverrunError, TimeoutError):
        if not connected:
            writer.write(b'HTTP/1.1 502 Bad Gateway\r\nContent-Length: 0\r\n\r\n')
            with contextlib.suppress(OSError):
                await writer.drain()
    finally:
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        if process and process.returncode is None:
            with contextlib.suppress(ProcessLookupError):
                process.terminate()
            await process.wait()
        writer.close()
        with contextlib.suppress(OSError):
            await writer.wait_closed()


async def main():
    """仅监听WSL回环，其他主机不可连接；日志只包含固定启动状态。"""
    server = await asyncio.start_server(handle, '127.0.0.1', PORT, limit=4096)
    print('DeepSeek本机直连通道已启动：WSL回环2082 → Windows原生TCP → 官方API443', flush=True)
    async with server:
        await server.serve_forever()


if __name__ == '__main__':
    if '--stdio' in sys.argv:
        windows_stdio()
    else:
        asyncio.run(main())

"""通过固定 stdio 子进程连接生图 MCP，不向模型提供命令执行入口。"""

import asyncio
import json


class ImageClient:
    def __init__(self, command):
        self.command = command
        self.process = None
        self.lock = asyncio.Lock()
        self.sequence = 0
        self.broken = False

    async def request(self, method, params):
        async with self.lock:
            if self.broken:
                raise RuntimeError("生图连接已中断，需人工检查，禁止重放")
            if self.process is None:
                self.process = await asyncio.create_subprocess_exec(
                    *self.command, stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.DEVNULL, limit=32 * 1024 * 1024)
            if self.process.returncode is not None:
                raise RuntimeError("生图进程已退出，禁止自动重放")
            self.sequence += 1
            message = {"jsonrpc": "2.0", "id": self.sequence, "method": method, "params": params}
            self.process.stdin.write((json.dumps(message) + "\n").encode())
            await self.process.stdin.drain()
            try:
                response = json.loads(await asyncio.wait_for(self.process.stdout.readline(), 20))
            except (ValueError, asyncio.TimeoutError):
                self.broken = True
                raise RuntimeError("生图连接中断，任务可能已提交") from None
            if response.get("id") != self.sequence or "error" in response:
                raise RuntimeError("生图 MCP 响应异常")
            return response["result"]

    async def call(self, args):
        return await self.request("tools/call", {"name": "chatgpt_image", "arguments": args})

    async def close(self):
        if self.process and self.process.returncode is None:
            self.process.stdin.close()
            try:
                await asyncio.wait_for(self.process.wait(), 5)
            except asyncio.TimeoutError:
                # 不强杀已经提交的图片任务；底层完成后随输入关闭退出。
                pass

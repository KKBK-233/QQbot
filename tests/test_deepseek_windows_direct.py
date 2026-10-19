"""离线验证本机通道的目标限制与TLS字节透明转发，不调用外部服务。"""
from pathlib import Path
import asyncio
import importlib.util
import unittest
from unittest.mock import patch

spec = importlib.util.spec_from_file_location('direct_bridge', Path(__file__).resolve().parents[1] / 'deepseek-windows-direct.py')
bridge = importlib.util.module_from_spec(spec)
spec.loader.exec_module(bridge)


class DirectTests(unittest.IsolatedAsyncioTestCase):
    async def test_arbitrary_hosts_ports_and_plain_http_are_denied(self):
        server = await asyncio.start_server(bridge.handle, '127.0.0.1', 0, limit=4096)
        async with server:
            with patch.object(bridge.asyncio, 'create_subprocess_exec') as spawn:
                for first in (b'CONNECT example.com:443 HTTP/1.1', b'CONNECT api.deepseek.com:80 HTTP/1.1', b'GET http://api.deepseek.com/ HTTP/1.1'):
                    reader, writer = await asyncio.open_connection('127.0.0.1', server.sockets[0].getsockname()[1])
                    writer.write(first + b'\r\n\r\n')
                    await writer.drain()
                    response = await reader.read()
                    self.assertTrue(response.startswith(b'HTTP/1.1 403'))
                    writer.close()
                    await writer.wait_closed()
                spawn.assert_not_called()

    async def test_connect_transfers_opaque_bytes_and_cleans_child(self):
        from types import SimpleNamespace
        incoming = asyncio.StreamReader()
        output = bytearray()
        server = await asyncio.start_server(bridge.handle, '127.0.0.1', 0, limit=4096)
        class Pipe:
            def write(self, data):
                output.extend(data)
            async def drain(self):
                pass
        async def wait():
            return 0
        def terminate():
            child.returncode = 0
        child = SimpleNamespace(stdout=incoming, stdin=Pipe(), returncode=None, wait=wait, terminate=terminate)
        incoming.feed_data(b'1')
        async with server:
            with patch.object(bridge.asyncio, 'create_subprocess_exec', return_value=child) as spawn:
                reader, writer = await asyncio.open_connection('127.0.0.1', server.sockets[0].getsockname()[1])
                writer.write(b'CONNECT api.deepseek.com:443 HTTP/1.1\r\n\r\n')
                await writer.drain()
                self.assertTrue((await reader.readuntil(b'\r\n\r\n')).startswith(b'HTTP/1.1 200'))
                payload = b'\x16\x03\x01opaque-tls-fixture'
                writer.write(payload)
                await writer.drain()
                for _ in range(20):
                    if output == payload:
                        break
                    await asyncio.sleep(.005)
                self.assertEqual(output, payload)
                incoming.feed_data(payload)
                self.assertEqual(await reader.readexactly(len(payload)), payload)
                incoming.feed_eof()
                await reader.read()
                writer.close()
                await writer.wait_closed()
                self.assertEqual(child.returncode, 0)
                self.assertNotIn('2080', str(spawn.call_args))


if __name__ == '__main__':
    unittest.main()

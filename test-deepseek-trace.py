"""离线验证诊断不改正文、保护凭据、区分保活等待并回收取消请求。"""
from pathlib import Path
import asyncio
import gzip
import importlib.util
import unittest

import httpx
from openai import AsyncOpenAI

spec = importlib.util.spec_from_file_location('deepseek_trace', Path(__file__).with_name('deepseek-transport-trace.py'))
trace = importlib.util.module_from_spec(spec)
spec.loader.exec_module(trace)


class Body(httpx.AsyncByteStream):
    def __init__(self, chunks, pause=0):
        self.chunks, self.pause, self.closed = chunks, pause, False

    async def __aiter__(self):
        for chunk in self.chunks:
            yield chunk
        if self.pause:
            await asyncio.sleep(self.pause)

    async def aclose(self):
        self.closed = True


class TraceTests(unittest.IsolatedAsyncioTestCase):
    async def test_sdk_scalar_timeout_does_not_override_connect_budget(self):
        seen = []
        def handler(request):
            seen.append(dict(request.extensions['timeout']))
            return httpx.Response(200, json={'id': 'test', 'object': 'chat.completion', 'created': 0,
                'model': 'deepseek-flash', 'choices': [{'index': 0, 'finish_reason': 'stop',
                    'message': {'role': 'assistant', 'content': 'OK'}}]})
        async with trace.TracedDeepSeekClient(report=lambda _: None, transport=httpx.MockTransport(handler)) as transport:
            sdk = AsyncOpenAI(api_key='test-secret', base_url='https://api.deepseek.com',
                             http_client=transport, timeout=120, max_retries=0)
            await sdk.chat.completions.create(model='deepseek-flash', messages=[{'role': 'user', 'content': 'test'}])
            await transport.get('https://api.deepseek.com/test', timeout=httpx.Timeout(120, connect=4))
            await transport.get('https://other.example/test', timeout=120)
        self.assertEqual(seen[0], {'connect': 10.0, 'read': 120, 'write': 120, 'pool': 120})
        self.assertEqual(seen[1]['connect'], 4)
        self.assertEqual(seen[1]['read'], 120)
        self.assertEqual(seen[2]['connect'], 120)

    async def test_sdk_retry_is_preserved_and_attempts_have_separate_ids(self):
        logs = []
        calls = 0
        def handler(request):
            nonlocal calls
            calls += 1
            if calls == 1:
                return httpx.Response(503, json={'error': {'message': 'temporary', 'type': 'server_error'}})
            return httpx.Response(200, json={'id': 'test', 'object': 'chat.completion', 'created': 0,
                'model': 'deepseek-flash', 'choices': [{'index': 0, 'finish_reason': 'stop',
                    'message': {'role': 'assistant', 'content': 'OK'}}]})
        async with trace.TracedDeepSeekClient(report=logs.append, transport=httpx.MockTransport(handler)) as transport:
            sdk = AsyncOpenAI(api_key='test-secret', base_url='https://api.deepseek.com',
                              http_client=transport, max_retries=1)
            result = await sdk.chat.completions.create(model='deepseek-flash', messages=[{'role': 'user', 'content': 'test'}])
            self.assertEqual(result.choices[0].message.content, 'OK')
        self.assertEqual(calls, 2)
        self.assertEqual(len(logs), 2)
        self.assertIn('HTTP=503', logs[0])
        self.assertIn('HTTP=200', logs[1])
        self.assertNotEqual(logs[0].split()[1], logs[1].split()[1])

    async def test_plain_response_is_preserved_and_credentials_are_not_logged(self):
        logs = []
        body = Body([b'\n\n', b'{"secret":"sk-do-not-log-this"}'])
        transport = httpx.MockTransport(lambda request: httpx.Response(200, stream=body))
        async with trace.TracedDeepSeekClient(report=logs.append, transport=transport) as client:
            response = await client.post('https://api.deepseek.com/chat/completions',
                                         headers={'Authorization': 'Bearer secret-key'}, content='private chat')
            self.assertEqual(response.content, b'\n\n{"secret":"sk-do-not-log-this"}')
        self.assertTrue(body.closed)
        self.assertEqual(len(logs), 1)
        self.assertIn('空白字节=2', logs[0])
        for secret in ('secret-key', 'private chat', 'sk-do-not-log-this'):
            self.assertNotIn(secret, ''.join(logs))

    async def test_stream_and_compressed_response_keep_original_semantics(self):
        logs = []
        payload = gzip.compress('中文正文'.encode())
        body = Body([payload])
        transport = httpx.MockTransport(lambda request: httpx.Response(200, headers={'content-encoding': 'gzip'}, stream=body))
        async with trace.TracedDeepSeekClient(report=logs.append, transport=transport) as client:
            async with client.stream('GET', 'https://api.deepseek.com/test') as response:
                self.assertEqual(logs, [])
                self.assertEqual(await response.aread(), '中文正文'.encode())
        self.assertTrue(body.closed)
        self.assertEqual(len(logs), 1)
        self.assertIn('压缩内容未判定', logs[0])

    async def test_cancellation_closes_body_and_stops_waiting_timer(self):
        logs = []
        body = Body([b'\n'], pause=30)
        transport = httpx.MockTransport(lambda request: httpx.Response(200, stream=body))
        async with trace.TracedDeepSeekClient(report=logs.append, diagnostic_interval=.01, transport=transport) as client:
            with self.assertRaises(TimeoutError):
                await asyncio.wait_for(client.get('https://api.deepseek.com/test'), timeout=.04)
        self.assertTrue(body.closed)
        self.assertTrue(any('等待中' in line and '首非空白=None' in line for line in logs))
        self.assertIn('CancelledError', logs[-1])
        count = len(logs)
        await asyncio.sleep(.03)
        self.assertEqual(len(logs), count)

    async def test_connection_failure_and_other_hosts_are_not_misreported(self):
        logs = []
        def failed(request):
            raise httpx.ConnectTimeout('contains-private-url-and-secret')
        async with trace.TracedDeepSeekClient(report=logs.append, transport=httpx.MockTransport(failed)) as client:
            with self.assertRaises(httpx.ConnectTimeout):
                await client.get('https://api.deepseek.com/test')
        self.assertIn('状态=ConnectTimeout', logs[0])
        self.assertIn('HTTP=None', logs[0])
        self.assertNotIn('contains-private', logs[0])
        logs.clear()
        transport = httpx.MockTransport(lambda request: httpx.Response(200, text='ok'))
        async with trace.TracedDeepSeekClient(report=logs.append, transport=transport) as client:
            self.assertEqual((await client.get('https://other.example/')).text, 'ok')
        self.assertEqual(logs, [])


if __name__ == '__main__':
    unittest.main()

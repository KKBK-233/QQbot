"""通过真实协议解析器及模拟 SSE 验证直请求，不调用真实模型或读取登录凭据。"""
import base64
from contextlib import nullcontext
import io
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch, Mock
import urllib.error

import codex_direct
from bridge import Bridge

SOURCE = Path(__file__).resolve().parents[1] / 'downloads/imagegen-research/image-use/image-use'
PNG = b'\x89PNG\r\n\x1a\nfixture-image'


def response(image=PNG, refusal=False):
    """流包含真实终态和可区分的图片字节，拒绝说明只留在本机诊断。"""
    item = {'type': 'image_generation_call', 'status': 'failed' if refusal else 'completed'}
    if not refusal:
        item['result'] = base64.b64encode(image).decode()
    output = [item]
    if refusal:
        output.append({'type': 'message', 'role': 'assistant', 'status': 'completed',
            'content': [{'type': 'output_text', 'text': '本机拒绝说明，不公开'}]})
    events = [{'type': 'response.created', 'response': {'model': 'gpt-6.1-sol'}},
              {'type': 'response.image_generation_call.generating'},
              {'type': 'response.output_item.done', 'item': item},
              {'type': 'response.completed', 'response': {'status': 'completed', 'output': output}}]
    return io.BytesIO(b''.join(('data: ' + json.dumps(e) + '\n\n').encode() for e in events))


class DirectTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.calls = []
        self.opener = Mock()
        self.opener.open.side_effect = self.open
        self.loader = codex_direct.load_runtime
        self.mode = 'success'
        self.refreshes = []

    def tearDown(self):
        self.temp.cleanup()

    def open(self, request, **kwargs):
        self.calls.append((request, kwargs))
        if self.mode == 'refresh' and len(self.calls) == 1:
            raise urllib.error.HTTPError(request.full_url, 401, 'unauthorized', {}, io.BytesIO(b'{}'))
        if self.mode == 'network':
            raise urllib.error.URLError('fixture disconnect')
        return response(refusal=self.mode == 'refusal')

    def load(self, source, proxy):
        runtime = self.loader(source, proxy)
        runtime['_load_auth'] = lambda: {'tokens': {'access_token': 'fixture-token',
            'refresh_token': 'fixture-refresh', 'account_id': 'fixture-account'}}
        runtime['_refresh_access_token'] = lambda token: self.refreshes.append(token) or {'access_token': 'new-fixture-token'}
        runtime['_persist_refreshed_auth'] = lambda *args: None
        runtime['_concurrency_slot'] = lambda *args: nullcontext()
        return runtime

    def generate(self, refs=()):
        with patch('codex_direct.urllib.request.build_opener', return_value=self.opener), \
             patch('codex_direct.load_runtime', side_effect=self.load), \
             patch('codex_direct.installed_version', return_value='0.160.1'), \
             patch('subprocess.run', side_effect=AssertionError('不得启动子进程')):
            return codex_direct.generate(SOURCE, '只改颜色', refs, 'gpt-6.1-sol', 'opaque',
                'http://127.0.0.1:2080', 2, self.root / 'diagnostic.json')

    def test_edit_uses_same_payload_and_returns_image_directly(self):
        ref = self.root / 'reference.png'
        ref.write_bytes(PNG)
        result = self.generate([ref])
        self.assertEqual(result.image, PNG)
        self.assertEqual(len(self.calls), 1)
        request = self.calls[0][0]
        self.assertEqual(request.full_url, 'https://chatgpt.com/backend-api/codex/responses')
        body = json.loads(request.data)
        expected = self.loader(SOURCE, 'http://127.0.0.1:2080')['_build_payload']('只改颜色', 'auto', 'png',
            'gpt-6.1-sol', refs=[(base64.b64encode(PNG).decode(), 'image/png')], n_character_refs=1, background='opaque')
        self.assertEqual(body, expected)
        self.assertEqual(result.trace['reference_count'], 1)
        self.assertTrue(result.trace['diagnostic_written'])

    def test_refusal_keeps_diagnostic_and_does_not_retry(self):
        self.mode = 'refusal'
        result = self.generate()
        self.assertIsNone(result.image)
        self.assertEqual(len(self.calls), 1)
        self.assertEqual(result.trace['image_call_statuses'], ['failed'])
        self.assertIn('本机拒绝说明', (self.root / 'diagnostic.json').read_text(encoding='utf-8'))
        self.assertNotIn('本机拒绝说明', json.dumps(result.trace, ensure_ascii=False))

    def test_authorization_refresh_preserves_protocol(self):
        self.mode = 'refresh'
        result = self.generate()
        self.assertEqual(result.image, PNG)
        self.assertEqual(len(self.calls), 2)
        self.assertEqual(self.refreshes, ['fixture-refresh'])
        self.assertEqual(result.trace['attempts'], 2)

    def test_network_failure_is_not_replayed_or_switched(self):
        self.mode = 'network'
        result = self.generate()
        self.assertIsNone(result.image)
        self.assertEqual(len(self.calls), 1)
        self.assertIn(b'network error', result.error_data)

    def test_explicit_proxy_does_not_change_process_environment(self):
        import os
        before = dict(os.environ)
        self.generate()
        self.assertEqual(dict(os.environ), before)

    def test_bridge_direct_failure_never_starts_legacy_subprocess(self):
        import hashlib
        bridge = Bridge({'backend': 'codex', 'codex_transport': 'direct', 'data_dir': str(self.root), 'image_use': str(SOURCE),
            'image_use_sha256': hashlib.sha256(SOURCE.read_bytes()).hexdigest()})
        try:
            with patch('bridge.codex_authorization_status', return_value={'local_oauth_present': True}), \
                 patch('bridge.generate_codex_direct', return_value=type('Outcome', (), {
                     'image': None, 'trace': {'attempts': 1}, 'error_data': b'no image returned'})()), \
                 patch('bridge.subprocess.run', side_effect=AssertionError('不得回退子进程')):
                bridge.run({'request_id': 'direct', 'created': 0}, '画猫', [])
            self.assertEqual(bridge.read('direct')['error_code'], 'no_image')
            self.assertFalse(bridge.read('direct')['auto_retry'])
            self.assertNotIn('backend_exit_code', bridge.read('direct'))
        finally:
            bridge.pool.shutdown()


if __name__ == '__main__':
    unittest.main()

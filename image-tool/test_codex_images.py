"""核验原生接口路由、引用字节、错误和授权边界；不读取真实凭据或消耗额度。"""
import base64
from contextlib import nullcontext
import io
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch
import urllib.error

import codex_images
from backend_trace import read_trace
from bridge import Bridge, backend_failure

SOURCE = Path(__file__).resolve().parents[1] / 'downloads/imagegen-research/image-use/image-use'
PNG = b'\x89PNG\r\n\x1a\nfixture-image'


class NativeTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.calls = []
        self.mode = 'success'
        self.auth = {'tokens': {'access_token': 'fixture-token', 'refresh_token': 'fixture-refresh'}}
        self.refreshes = []
        self.slots = []
        self.opener = Mock()
        self.opener.open.side_effect = self.open
        self.loader = codex_images.load_runtime

    def tearDown(self):
        self.temp.cleanup()

    def load(self, source, proxy):
        runtime = self.loader(source, proxy)
        runtime['_load_auth'] = lambda: self.auth
        runtime['_concurrency_slot'] = lambda *args: self.slots.append(args[:2]) or nullcontext()
        runtime['_refresh_access_token'] = lambda token: self.refreshes.append(token) or {'access_token': 'new-token'}
        runtime['_persist_refreshed_auth'] = lambda auth, value: auth['tokens'].update(value)
        runtime['run_codex'] = Mock(side_effect=AssertionError('不得调用文字驱动'))
        return runtime

    def open(self, request, **kwargs):
        self.calls.append(request)
        if self.mode == 'refresh' and len(self.calls) == 1:
            raise urllib.error.HTTPError(request.full_url, 401, 'unauthorized', {}, io.BytesIO(b'{}'))
        if self.mode in ('quota', 'refusal'):
            code = 'rate_limit_exceeded' if self.mode == 'quota' else 'moderation_blocked'
            body = json.dumps({'error': {'code': code, 'message': '本机说明 Bearer secret-token'}}).encode()
            raise urllib.error.HTTPError(request.full_url, 429 if self.mode == 'quota' else 403, '', {}, io.BytesIO(body))
        if self.mode == 'network':
            raise urllib.error.URLError('fixture disconnect')
        value = {'data': [{'b64_json': base64.b64encode(PNG).decode()}], 'quality': 'medium',
                 'size': '1254x1254', 'background': 'opaque', 'safety_identifier': 'private-id'}
        if self.mode == 'no_image':
            value['data'] = []
        if self.mode == 'bad_image':
            value['data'] = [{'b64_json': '%%%'}]
        if self.mode == 'reported_model':
            value['model'] = 'gpt-image-2-codex'
        response = io.BytesIO(json.dumps(value).encode())
        response.status = 200
        return response

    def generate(self, refs=()):
        with patch('codex_direct.urllib.request.build_opener', return_value=self.opener), \
             patch('codex_images.load_runtime', side_effect=self.load), \
             patch('codex_images.installed_version', return_value='0.160.1'), \
             patch('subprocess.run', side_effect=AssertionError('不得启动子进程')):
            return codex_images.generate(SOURCE, '只改颜色', refs, 'gpt-image-2.5-sunburst', 'opaque',
                'http://127.0.0.1:2080', 2, self.root / 'diagnostic.json', quality='high')

    def test_generation_has_no_text_model_and_keeps_requested_vs_returned_distinct(self):
        result = self.generate()
        self.assertEqual(result.image, PNG)
        self.assertEqual(len(self.calls), 1)
        request = self.calls[0]
        self.assertTrue(request.full_url.endswith('/codex/images/generations'))
        body = json.loads(request.data)
        self.assertEqual(body['model'], 'gpt-image-2.5-sunburst')
        self.assertEqual(body['quality'], 'high')
        for key in ('tools', 'input', 'reasoning', 'images'):
            self.assertNotIn(key, body)
        self.assertEqual(self.slots, [('codex', 2)])
        trace = read_trace(('QQBOT_IMAGE_TRACE ' + json.dumps(result.trace)).encode())
        self.assertEqual(trace['requested_image']['quality'], 'high')
        self.assertEqual(trace['rendered_image']['quality'], 'medium')
        self.assertFalse(trace['model_reported'])
        self.assertNotIn('effective_image', trace)
        self.assertNotIn('requested_driver_model', trace)
        self.assertEqual(trace['http_status'], 200)
        self.assertNotIn('private-id', json.dumps(result.trace))

    def test_edit_inlines_actual_reference_bytes_in_order(self):
        refs = [self.root / 'a.png', self.root / 'b.png']
        for index, path in enumerate(refs):
            path.write_bytes(PNG + str(index).encode())
        result = self.generate(refs)
        self.assertEqual(result.image, PNG)
        self.assertTrue(self.calls[0].full_url.endswith('/codex/images/edits'))
        entries = json.loads(self.calls[0].data)['images']
        for path, entry in zip(refs, entries):
            self.assertEqual(base64.b64decode(entry['image_url'].split(',', 1)[1]), path.read_bytes())
        self.assertEqual(result.trace['reference_count'], 2)
        diagnostic = (self.root / 'diagnostic.json').read_text(encoding='utf-8')
        self.assertNotIn('base64', diagnostic)
        self.assertNotIn('只改颜色', diagnostic)

    def test_401_refreshes_once_before_success(self):
        self.mode = 'refresh'
        result = self.generate()
        self.assertEqual(result.image, PNG)
        self.assertEqual(len(self.calls), 2)
        self.assertEqual(self.refreshes, ['fixture-refresh'])
        self.assertEqual(result.trace['attempts'], 2)
        self.assertEqual(self.calls[1].get_header('Authorization'), 'Bearer new-token')

    def test_quota_and_content_rejection_do_not_retry_or_fall_back(self):
        for mode, expected in (('quota', 'quota'), ('refusal', 'content_rejected')):
            self.mode = mode
            self.calls.clear()
            result = self.generate()
            self.assertIsNone(result.image)
            self.assertEqual(len(self.calls), 1)
            self.assertEqual(backend_failure(result.error_data)[0], expected)
            self.assertNotIn('secret-token', (self.root / 'diagnostic.json').read_text(encoding='utf-8'))

    def test_invalid_reference_is_rejected_before_network(self):
        ref = self.root / 'evil.png'
        ref.write_bytes(b'<html>not an image</html>')
        result = self.generate([ref])
        self.assertIsNone(result.image)
        self.assertEqual(len(self.calls), 0)

    def test_network_and_missing_or_corrupt_image_never_replay(self):
        for mode in ('network', 'no_image', 'bad_image'):
            self.mode = mode
            self.calls.clear()
            result = self.generate()
            self.assertIsNone(result.image)
            self.assertEqual(len(self.calls), 1)

    def test_only_explicit_server_model_is_reported(self):
        self.mode = 'reported_model'
        result = self.generate()
        self.assertEqual(result.trace['effective_image']['model'], 'gpt-image-2-codex')
        self.assertTrue(result.trace['model_reported'])

    def test_proxy_is_local_to_request_and_does_not_change_environment(self):
        import os
        before = dict(os.environ)
        self.generate()
        self.assertEqual(dict(os.environ), before)

    def test_bridge_native_uses_admin_quality_and_parent_snapshot(self):
        import hashlib
        bridge = Bridge({'backend': 'codex', 'data_dir': str(self.root), 'image_use': str(SOURCE),
                         'image_use_sha256': hashlib.sha256(SOURCE.read_bytes()).hexdigest()})
        try:
            self.assertEqual(bridge.codex_transport, 'native')
            bridge.save({'request_id': 'parent', 'state': 'succeeded'})
            (self.root / 'parent.png').write_bytes(PNG)
            with patch('bridge.codex_authorization_status', return_value={'local_oauth_present': True}), \
                 patch('bridge.generate_codex_images', return_value=type('Outcome', (), {
                     'image': PNG, 'trace': {}, 'error_data': b''})()) as generate, \
                 patch('bridge.generate_codex_direct', side_effect=AssertionError('不得调用旧文字驱动')):
                bridge.call({'action': 'edit', 'request_id': 'child', 'parent_id': 'parent', 'prompt': '只改颜色'})
                bridge.pool.shutdown(wait=True)
            args = generate.call_args.args
            self.assertEqual(args[2][0].read_bytes(), PNG)
            self.assertEqual(args[3], 'gpt-image-2.5-sunburst')
            self.assertEqual(generate.call_args.kwargs, {'quality': 'high'})
            self.assertEqual(bridge.read('child')['state'], 'succeeded')
            self.assertEqual(bridge.read('child')['backend_transport'], 'native')
        finally:
            bridge.pool.shutdown(wait=True)


if __name__ == '__main__':
    unittest.main()

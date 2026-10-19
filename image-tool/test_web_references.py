"""验证公网参考图下载边界、会话隔离、过期与摘要，测试不联网不生图。"""
import asyncio
import base64
import importlib.util
import json
from pathlib import Path
import tempfile
import time
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, patch

folder = Path(__file__).parent / 'maibot-plugin'
spec = importlib.util.spec_from_file_location('web_references', folder / 'web_references.py')
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)


class ReferenceTests(unittest.IsolatedAsyncioTestCase):
    def test_session_expiry_digest_and_invalid_ids(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            store = module.WebReferences(root / 'metadata', root / 'inbox')
            raw = b'\x89PNG\r\n\x1a\n' + b'fixture-only'
            item = store.store('group-a', {'title': '原图', 'url': 'https://example.com/source',
                                          'image': 'https://example.com/image.png'}, raw, {'width': 800, 'height': 900})
            rid = item['reference_id']
            self.assertEqual((root / 'inbox' / store.resolve('group-a', rid)).read_bytes(), raw)
            for value in ('../secret', 'https://example.com/image.png', 'invented'):
                with self.assertRaises(ValueError):
                    store.resolve('group-a', value)
            with self.assertRaises(ValueError):
                store.resolve('group-b', rid)
            path = store.root / (rid + '.json')
            original = json.loads(path.read_text())
            expired = dict(original, expires=time.time() - 1)
            path.write_text(json.dumps(expired))
            with self.assertRaises(ValueError):
                store.resolve('group-a', rid)
            path.write_text(json.dumps(original))
            (store.inbox / original['file']).write_bytes(b'changed')
            with self.assertRaises(ValueError):
                store.resolve('group-a', rid)

    async def test_search_downloads_original_and_imports_exact_bytes(self):
        with tempfile.TemporaryDirectory() as temporary:
            store = module.WebReferences(Path(temporary) / 'metadata', Path(temporary) / 'inbox')
            reader = SimpleNamespace(validate_url=lambda url: (url, None, None, None))
            search = SimpleNamespace(sandbox_command=lambda worker: ['/usr/bin/bwrap', str(worker)])
            rows = {'success': True, 'results': [{'title': '角色原图', 'image': 'https://example.com/original.png', 'url': 'https://example.com/source'}]}
            raw = b'original-image-bytes'
            with patch.object(module, 'web_modules', return_value=(search, reader)), \
                 patch.object(module, 'download', AsyncMock(return_value=raw)) as download, \
                 patch.object(module, 'run_worker', AsyncMock(side_effect=[rows, {'success': True, 'width': 800, 'height': 900}])) as worker:
                result = await store.search('group-a', '角色 原作')
            self.assertTrue(result['success'])
            download.assert_awaited_once_with(reader, 'https://example.com/original.png', 'http://127.0.0.1:2081')
            self.assertIn('--unshare-net', worker.await_args_list[1].args[0])
            self.assertEqual(base64.b64decode(worker.await_args_list[1].args[1]['image']), raw)
            self.assertEqual((store.inbox / store.resolve('group-a', result['references'][0]['reference_id'])).read_bytes(), raw)

    async def test_missing_original_does_not_report_reference_ready(self):
        with tempfile.TemporaryDirectory() as temporary:
            store = module.WebReferences(Path(temporary) / 'metadata', Path(temporary) / 'inbox')
            reader = SimpleNamespace(validate_url=lambda url: (url, None, None, None))
            search = SimpleNamespace(sandbox_command=lambda worker: ['/usr/bin/bwrap', str(worker)])
            rows = {'success': True, 'results': [{'title': '角色', 'image': 'https://example.com/image', 'url': 'https://example.com'}]}
            with patch.object(module, 'web_modules', return_value=(search, reader)), \
                 patch.object(module, 'run_worker', AsyncMock(return_value=rows)), \
                 patch.object(module, 'download', AsyncMock(side_effect=ValueError('HTML'))):
                with self.assertRaises(ValueError):
                    await store.search('group-a', '角色')
            self.assertEqual(list(store.root.glob('*.json')), [])

    async def test_redirect_is_checked_before_second_download(self):
        def validate(url):
            if '127.0.0.1' in url:
                raise ValueError('内网')
            return (url, None, None, None)
        fetch = AsyncMock(return_value=(302, {'location': 'http://127.0.0.1/private'}, b''))
        reader = SimpleNamespace(validate_url=validate, fetch_once=fetch)
        with self.assertRaises(ValueError):
            await module.download(reader, 'https://example.com/image', '')
        self.assertEqual(fetch.await_count, 1)
        self.assertTrue(fetch.await_args.kwargs['image'])

    async def test_query_credentials_never_start_search(self):
        with tempfile.TemporaryDirectory() as temporary, patch.object(module, 'web_modules') as loader:
            store = module.WebReferences(Path(temporary) / 'metadata', Path(temporary) / 'inbox')
            for query in ('', 'x' * 201, 'sk-' + 'x' * 24):
                with self.assertRaises(ValueError):
                    await store.search('group-a', query)
            loader.assert_not_called()


if __name__ == '__main__':
    unittest.main()

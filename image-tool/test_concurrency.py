"""以阻塞的模拟后端验证真正重叠执行、限流、串行开关与任务隔离，不生图不发QQ。"""
import hashlib
import json
from pathlib import Path
import tempfile
import threading
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from bridge import Bridge, backend_environment

PNG = b'\x89PNG\r\n\x1a\n' + b'fixture-only-'


class ConcurrencyTests(unittest.TestCase):
    def exercise(self, workers):
        """同时提交三项，阻塞第一批任务，以状态与峰值验证调度，而非仅检查线程数。"""
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            source = root / 'source'
            source.write_bytes(b'fixture-source')
            bridge = Bridge({'backend': 'codex', 'max_concurrent': workers, 'data_dir': folder,
                             'image_use': str(source), 'image_use_sha256': hashlib.sha256(source.read_bytes()).hexdigest()})
            entered = threading.Event()
            release = threading.Event()
            guard = threading.Lock()
            active = 0
            peak = 0
            seen = []

            def backend(source, prompt, refs, model, background, proxy, concurrency, diagnostic_path, *, quality):
                nonlocal active, peak
                rid = diagnostic_path.parent.name
                reference = refs[0]
                self.assertEqual(reference.read_bytes(), PNG + rid.encode())
                self.assertIn('画' + rid, prompt)
                self.assertEqual(concurrency, workers)
                self.assertEqual(model, 'gpt-image-2.5-sunburst')
                self.assertEqual(quality, 'high')
                self.assertEqual(diagnostic_path, root / rid / 'backend-diagnostic.json')
                with guard:
                    active += 1
                    peak = max(peak, active)
                    seen.append(rid)
                    if active == workers:
                        entered.set()
                try:
                    self.assertTrue(release.wait(3), '调度测试没有释放模拟后端')
                    return SimpleNamespace(image=PNG + rid.encode(), trace={}, error_data=b'')
                finally:
                    with guard:
                        active -= 1

            try:
                with patch('bridge.codex_authorization_status', return_value={'local_oauth_present': True}), \
                     patch('bridge.generate_codex_images', side_effect=backend), \
                     patch('bridge.subprocess.run', side_effect=AssertionError('直请求不得启动子进程')):
                    for rid in ('first', 'second', 'third'):
                        (root / 'inbox' / (rid + '.png')).write_bytes(PNG + rid.encode())
                        args = {'action': 'edit', 'request_id': rid, 'prompt': '画' + rid, 'references': [rid + '.png']}
                        bridge.call(args)
                        bridge.call(args)
                        # 父任务未完成时仍不能抢先编辑，独立新图才允许并发。
                        with self.assertRaises(ValueError):
                            bridge.call({'action': 'edit', 'request_id': rid + '-child', 'prompt': '只改颜色', 'parent_id': rid})
                    self.assertTrue(entered.wait(2), '第一批任务没有同时进入后端')
                    states = [bridge.read(rid)['state'] for rid in ('first', 'second', 'third')]
                    self.assertEqual(states.count('running'), workers)
                    self.assertEqual(states.count('queued'), 3 - workers)
                    # 排满后的拒绝不会影响已提交任务，也不会放宽并发上限。
                    with self.assertRaises(ValueError):
                        bridge.call({'action': 'generate', 'request_id': 'fourth', 'prompt': '猫'})
                    release.set()
                    bridge.pool.shutdown(wait=True)
                self.assertEqual(peak, workers)
                self.assertEqual(sorted(seen), ['first', 'second', 'third'])
                for rid in seen:
                    self.assertEqual(bridge.read(rid)['state'], 'succeeded')
                    self.assertEqual((root / (rid + '.png')).read_bytes(), PNG + rid.encode())
                events = [json.loads(line) for line in (root / 'image-events.jsonl').read_text().splitlines()]
                self.assertTrue(all(event['max_concurrent'] == workers for event in events))
            finally:
                release.set()
                bridge.pool.shutdown(wait=True)

    def test_two_codex_jobs_overlap_without_mixing_references_or_results(self):
        self.exercise(2)

    def test_admin_can_restore_serial_execution(self):
        self.exercise(1)

    def test_backend_limits_and_external_environment_cannot_disable_gate(self):
        for backend, limit in (('web', 2), ('codex', 3), ('codex', 0), ('codex', True), ('codex', '2')):
            with tempfile.TemporaryDirectory() as folder, self.assertRaises(ValueError):
                Bridge({'backend': backend, 'max_concurrent': limit, 'data_dir': folder})
        env = backend_environment('codex', {'IMAGE_USE_CODEX_CONCURRENCY': '0', 'CHATGPT_IMAGEGEN_CODEX_CONCURRENCY': '0'})
        self.assertEqual(env['IMAGE_USE_CODEX_CONCURRENCY'], '2')
        self.assertNotIn('CHATGPT_IMAGEGEN_CODEX_CONCURRENCY', env)
        with tempfile.TemporaryDirectory() as folder:
            for backend, expected in (('codex', 2), ('web', 1)):
                bridge = Bridge({'backend': backend, 'data_dir': folder})
                try:
                    self.assertEqual(bridge.max_concurrent, expected)
                finally:
                    bridge.pool.shutdown()


if __name__ == '__main__':
    unittest.main()

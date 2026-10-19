"""核对诊断日志不泄露正文，日志故障不影响任务持久化。"""

import json
from pathlib import Path
import tempfile
import unittest

from bridge import Bridge
from inspect_jobs import collect


class JobLogTests(unittest.TestCase):
    def test_metadata_only_and_receipt_join(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            bridge = Bridge({'backend': 'codex', 'data_dir': folder})
            try:
                bridge.save({'request_id': 'test', 'state': 'succeeded', 'created': 10,
                             'prompt': 'private-message', 'error': 'secret-raw-error',
                             'backend_trace': {'attempts': 1, 'accepted': True, 'token': 'secret-token'}})
                raw = (root / 'image-events.jsonl').read_text(encoding='utf-8')
                for secret in ('private-message', 'secret-raw-error', 'secret-token'):
                    self.assertNotIn(secret, raw)
                self.assertEqual(json.loads(raw)['backend_trace']['attempts'], 1)
                delivery = root / 'delivery'
                delivery.mkdir()
                (delivery / 'test.json').write_text('{"state":"sent","elapsed_seconds":2}', encoding='utf-8')
                row = collect(root, delivery, 1)[0]
                self.assertEqual(row['delivery_state'], 'sent')
                self.assertEqual(row['delivery_seconds'], 2)
                self.assertNotIn('error', row)
                (root / 'image-events.jsonl').unlink()
                (root / 'image-events.jsonl').mkdir()
                bridge.save({'request_id': 'test', 'state': 'uncertain'})
                self.assertEqual(bridge.read('test')['state'], 'uncertain')
            finally:
                bridge.pool.shutdown()


if __name__ == '__main__':
    unittest.main()

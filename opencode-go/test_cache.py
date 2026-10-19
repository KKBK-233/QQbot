"""缓存探针离线回归：验证统计分母、断点、会话和不自动重试。"""
from contextlib import redirect_stdout
import io
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import httpx

import verify_cache as probe


class CacheTests(unittest.TestCase):
    """只使用模拟HTTP与临时假凭据，不联网、不读取真实密钥。"""

    def test_cold_and_warm_rates(self):
        rows = [{'total_input_tokens': 9630, 'cache_read_input_tokens': value} for value in [0, 9600, 9600]]
        result = probe.summarize(rows)
        self.assertEqual(result['request_hit_rate_percent'], 66.67)
        self.assertEqual(result['token_hit_rate_percent'], 66.46)
        self.assertEqual(result['warm_request_hit_rate_percent'], 100)
        self.assertEqual(result['warm_token_hit_rate_percent'], 99.69)

    def test_empty_statistics_are_not_zero(self):
        result = probe.summarize([])
        self.assertIsNone(result['token_hit_rate_percent'])
        self.assertIsNone(result['request_hit_rate_percent'])

    def test_only_explicit_prefix_marked(self):
        baseline = probe.probe_payload('test', 'baseline', 0)
        first = probe.probe_payload('test', 'explicit', 0)
        second = probe.probe_payload('test', 'explicit', 1)
        self.assertNotIn('cache_control', baseline['system'][0])
        self.assertEqual(first['system'][0]['cache_control'], {'type': 'ephemeral'})
        self.assertEqual(first['system'], second['system'])
        self.assertNotEqual(first['messages'], second['messages'])

    def run_probe(self, status=200, missing_counts=False):
        """构造临时配置与HTTP服务，不触碰宿主数据。"""
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            (root / 'config').mkdir()
            (root / 'config/model_config.toml').write_text('[model_task_config.planner]\nmodel_list=["unchanged"]\n')
            key = root / 'fake-key'
            key.write_text('oc_sk_FAKE_TEST_ONLY')
            key.chmod(0o600)
            requests = []
            original = httpx.Client
            def handler(request):
                requests.append(request)
                if status != 200:
                    return httpx.Response(status, json={'error': {'message': 'SECRET_BODY'}})
                usage = {'input_tokens': 100, 'output_tokens': 1}
                if not missing_counts:
                    usage.update(cache_creation_input_tokens=0, cache_read_input_tokens=0)
                return httpx.Response(200, json={'model': probe.MODEL, 'stop_reason': 'end_turn',
                                      'content': [{'type': 'text', 'text': 'CACHE_PROBE_OK'}], 'usage': usage})
            def factory(**kwargs):
                self.assertEqual(kwargs.pop('proxy'), 'http://127.0.0.1:2081')
                self.assertFalse(kwargs['trust_env'])
                self.assertFalse(kwargs['follow_redirects'])
                return original(transport=httpx.MockTransport(handler), **kwargs)
            output = io.StringIO()
            with patch.object(probe, 'ROOT', root), patch.object(probe, 'KEY_FILE', key), \
                 patch.object(probe.httpx, 'Client', factory), redirect_stdout(output):
                result = probe.main(3)
            reports = list((root / 'data/opencode-go').glob('cache-probe-*.json'))
            saved = json.loads(reports[0].read_text())
            self.assertNotIn('FAKE_TEST_ONLY', reports[0].read_text())
            self.assertNotIn('SECRET_BODY', output.getvalue())
            return result, requests, saved

    def test_same_session_and_no_config_switch(self):
        result, requests, saved = self.run_probe()
        self.assertEqual(result, 0)
        self.assertEqual(len(requests), 6)
        self.assertEqual(len({request.headers['x-opencode-session'] for request in requests}), 1)
        self.assertFalse(saved['production_tasks_changed'])

    def test_failure_stops_without_retry(self):
        result, requests, saved = self.run_probe(status=403)
        self.assertEqual(result, 1)
        self.assertEqual(len(requests), 1)
        self.assertFalse(saved['success'])

    def test_missing_counters_are_not_zero(self):
        result, requests, saved = self.run_probe(missing_counts=True)
        self.assertEqual(result, 1)
        self.assertEqual(len(requests), 1)
        self.assertNotIn('cache_read_input_tokens', saved['groups']['baseline']['rows'][0])


if __name__ == '__main__':
    unittest.main()

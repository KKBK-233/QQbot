"""模拟完整生图子进程与状态接口，验证失败诊断落盘且正文不进入群聊返回。"""
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import time
import unittest
from unittest.mock import patch

from bridge import Bridge


# 模拟上游仅发送正常终态及失败工具，不连接网络，也不执行真实模型。
SOURCE = '''
WEB_SEND_SEL = ""
WEB_SEND_CLICK_SEL = ""
class GatewayError(Exception):
    pass
def _ab(*args, **kwargs):
    pass
def _detect_codex_version():
    return "0.0.0"
def _build_payload(*args, **kwargs):
    return {"model": "gpt-fixture", "input": []}
def _stream(*args):
    yield {"type": "response.created", "response": {"status": "in_progress"}}
    yield {"type": "response.image_generation_call.generating"}
    yield {"type": "response.completed", "response": {"status": "completed", "output": [
        {"type": "image_generation_call", "status": "failed", "result": None},
        {"type": "message", "role": "assistant", "content": [
            {"type": "refusal", "refusal": "本机诊断专用说明"}]}]}}
def main():
    for event in _stream("fixture", {}, _build_payload(), 1, 1):
        pass
    raise SystemExit("no image returned")
'''


class DiagnosticFlowTests(unittest.TestCase):
    @unittest.skipUnless(os.name == 'nt', '仅Windows的stdio管道继承会触发本次阻塞')
    def test_runner_finishes_while_mcp_input_pipe_stays_open(self):
        """保持真实MCP输入管道打开，验证模拟后端不被服务的输入读取阻塞。"""
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            source = root / 'fixture.py'
            source.write_text(SOURCE, encoding='utf-8')
            config = root / 'config.json'
            config.write_text(json.dumps({'backend': 'codex', 'codex_transport': 'subprocess', 'data_dir': str(root / 'jobs'),
                'image_use': str(source), 'chrome_use': str(source),
                'image_use_sha256': hashlib.sha256(source.read_bytes()).hexdigest()}), encoding='utf-8')
            # 仅模拟流，鉴权也使用替身；限制等待，回归失败不需要等生产的660秒。
            bootstrap = f'''
import sys
sys.path.insert(0, {str(Path(__file__).resolve().parent)!r})
import bridge
original_run = bridge.subprocess.run
def bounded_run(*args, **kwargs):
    kwargs['timeout'] = min(kwargs.get('timeout', 5), 5)
    return original_run(*args, **kwargs)
bridge.subprocess.run = bounded_run
bridge.codex_authorization_status = lambda: {{'local_oauth_present': True}}
bridge.main()
'''
            with tempfile.TemporaryFile() as errors:
                process = subprocess.Popen([sys.executable, '-c', bootstrap, '--config', str(config)],
                    stdin=subprocess.PIPE, stdout=subprocess.DEVNULL, stderr=errors,
                    creationflags=subprocess.CREATE_NO_WINDOW)
                try:
                    request = {'jsonrpc': '2.0', 'id': 1, 'method': 'tools/call', 'params': {
                        'name': 'chatgpt_image', 'arguments': {'action': 'generate',
                        'request_id': 'stdin-fixture', 'prompt': '本地模拟，不调用真实模型'}}}
                    process.stdin.write((json.dumps(request) + '\n').encode('utf-8'))
                    process.stdin.flush()
                    job_path = root / 'jobs/stdin-fixture.json'
                    deadline = time.monotonic() + 8
                    saved = {}
                    while time.monotonic() < deadline:
                        if job_path.exists():
                            saved = json.loads(job_path.read_text(encoding='utf-8'))
                            if saved['state'] not in {'queued', 'running'}:
                                break
                        if process.poll() is not None:
                            errors.seek(0)
                            self.fail(errors.read(2048).decode('utf-8', errors='replace'))
                        time.sleep(0.05)
                    # 模拟返回故意没有图片；必须得到响应诊断，不能在子进程启动阶段超时。
                    self.assertEqual(saved.get('error_code'), 'no_image', saved)
                    self.assertTrue(saved['backend_trace']['diagnostic_written'])
                    self.assertEqual(saved['backend_trace']['attempts'], 1)
                finally:
                    process.stdin.close()
                    try:
                        process.wait(timeout=7)
                    except subprocess.TimeoutExpired:
                        process.kill()
                        process.wait(timeout=3)

    def test_runner_saves_terminal_response_without_exposing_text_or_retrying(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            source = root / 'fixture.py'
            source.write_text(SOURCE, encoding='utf-8')
            bridge = Bridge({'backend': 'codex', 'codex_transport': 'subprocess', 'data_dir': str(root / 'jobs'),
                'image_use': str(source), 'image_use_sha256': hashlib.sha256(source.read_bytes()).hexdigest()})
            try:
                job = {'request_id': 'fixture', 'state': 'queued', 'created': int(time.time())}
                with patch('bridge.codex_authorization_status', return_value={'local_oauth_present': True}):
                    bridge.run(job, '不应记录的输入提示词', [])
                saved = bridge.read('fixture')
                self.assertEqual(saved['state'], 'uncertain')
                self.assertEqual(saved['error_code'], 'no_image')
                self.assertFalse(saved['auto_retry'])
                self.assertTrue(saved['backend_trace']['diagnostic_written'])
                self.assertTrue(saved['backend_trace']['refusal_present'])
                self.assertEqual(saved['backend_trace']['image_call_statuses'], ['failed'])
                diagnostic = json.loads((bridge.root / 'fixture' / 'backend-diagnostic.json').read_text(encoding='utf-8'))
                self.assertIn('本机诊断专用说明', str(diagnostic))
                with patch('bridge.codex_authorization_status', return_value={'local_oauth_present': True}):
                    status = bridge.call({'action': 'status', 'request_id': 'fixture'})
                public = json.dumps(status, ensure_ascii=False) + (bridge.root / 'image-events.jsonl').read_text(encoding='utf-8')
                self.assertNotIn('本机诊断专用说明', public)
                self.assertNotIn('不应记录的输入提示词', public)
                self.assertNotIn('不应记录的输入提示词', str(diagnostic))
                self.assertEqual(saved['backend_trace']['attempts'], 1)
            finally:
                bridge.pool.shutdown()


if __name__ == '__main__':
    unittest.main()

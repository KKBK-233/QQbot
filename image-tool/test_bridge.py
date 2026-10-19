"""验证工具权限边界、逐轮编辑和幂等，测试不调用浏览器或消耗生图额度。"""

import json
import hashlib
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from bridge import Bridge, rpc, backend_failure, codex_authorization_status, VISUAL_STRUCTURE, backend_environment, background_mode


PNG = b"\x89PNG\r\n\x1a\n" + b"fixture-only-not-real-image"


class BridgeTests(unittest.TestCase):
    def test_background_defaults_cover_edits_and_explicit_transparency(self):
        self.assertEqual(background_mode('Q版聊天表情包，简单背景', False), 'opaque')
        self.assertEqual(background_mode('只改表情，其他保持', True), 'opaque')
        self.assertEqual(background_mode('请画透明背景贴纸', True), 'transparent')
        self.assertEqual(background_mode('请画不透明背景', True), 'opaque')
        self.assertEqual(background_mode('remove the background', True), 'transparent')
        self.assertEqual(background_mode('保留原图透明度，只改表情', True), 'transparent')
        self.assertEqual(background_mode('不要抠图，补上街景', True), 'opaque')

    def test_latest_reference_edit_requests_opaque_canvas(self):
        self.bridge.backend = 'codex'
        args = self.bridge.command('regression', '背景与钞票布置保持原样，只把猫换成DS娘', [Path('role.png'), Path('original.png')])
        self.assertEqual(args[args.index('--background') + 1], 'opaque')
        self.assertIn('已有实景背景', args[-1])
        self.assertIn('全画布不透明', args[-1])
        self.assertIn('具体场景', args[-1])
        self.assertEqual(args.count('--ref'), 2)

    def test_admin_driver_override_is_explicit_and_cannot_be_set_by_tool(self):
        self.bridge.backend = 'codex'
        self.bridge.driver_model = 'gpt-6-astra'
        args = self.bridge.command('fixture', '猫', [])
        self.assertEqual(args[args.index('--model') + 1], 'gpt-6-astra')
        with self.assertRaises(ValueError):
            self.bridge.call({'action': 'generate', 'request_id': 'x', 'prompt': '猫', 'codex_driver_model': 'gpt-5.5'})

    def test_explicit_transparent_edit_does_not_receive_opaque_prompt(self):
        self.bridge.backend = 'codex'
        args = self.bridge.command('transparent', '保留原图透明度，只改表情', [Path('original.png')])
        self.assertEqual(args[args.index('--background') + 1], 'transparent')
        self.assertNotIn('全画布不透明', args[-1])

    def test_edit_job_background_matches_subprocess_request(self):
        """实际参考图任务记录和后端参数一致，排队测试不会启动生图。"""
        self.bridge.backend = 'codex'
        (self.bridge.root / 'inbox' / 'original.png').write_bytes(PNG)
        for rid, prompt, expected in (
            ('full-scene', '背景与钞票布置保持原样，只把猫换成DS娘', 'opaque'),
            ('cutout', '保留原图透明度，只改表情', 'transparent'),
        ):
            job = self.bridge.call({'action': 'edit', 'request_id': rid, 'prompt': prompt,
                                    'references': ['original.png']})
            args = self.bridge.command(rid, prompt, [self.bridge.root / rid / 'ref-0.png'])
            self.assertEqual(job['requested_background'], expected)
            self.assertEqual(self.bridge.read(rid)['requested_background'], expected)
            self.assertEqual(args[args.index('--background') + 1], expected)
        self.assertEqual(self.submit.call_count, 2)

    def test_codex_network_overrides_conflicting_proxies_without_mutating_parent(self):
        parent = {'https_proxy': 'http://stale:7890', 'HTTPS_PROXY': 'http://wsl:2081',
                  'ALL_PROXY': 'socks5://stale:7890', 'no_proxy': 'chatgpt.com',
                  'IMAGE_USE_CODEX_MODEL': 'override', 'PATH': 'fixture-path'}
        before = dict(parent)
        env = backend_environment('codex', parent)
        self.assertEqual(env['HTTPS_PROXY'], 'http://127.0.0.1:2080')
        self.assertEqual(env['HTTP_PROXY'], env['HTTPS_PROXY'])
        self.assertNotIn('chatgpt.com', env['NO_PROXY'])
        self.assertNotIn('https_proxy', env)
        self.assertNotIn('ALL_PROXY', env)
        self.assertNotIn('IMAGE_USE_CODEX_MODEL', env)
        self.assertEqual(env['PATH'], parent['PATH'])
        self.assertEqual(parent, before)
        self.assertEqual(backend_environment('web', parent)['https_proxy'], parent['https_proxy'])

    def test_status_reports_current_auth_without_retrying_old_failure(self):
        self.bridge.backend = 'codex'
        job = {'request_id': 'old', 'state': 'failed', 'error': '缺少 Codex ChatGPT 登录授权，请先 codex login'}
        self.bridge.save(job)
        original = self.bridge.record('old').read_bytes()
        with patch('bridge.codex_authorization_status', return_value={'local_oauth_present': True, 'server_verified': False}):
            status = self.bridge.call({'action': 'status', 'request_id': 'old'})
        self.assertEqual(status['state'], 'failed')
        self.assertTrue(status['current_authorization']['local_oauth_present'])
        self.assertIn('目前已发现凭据', status['status_note'])
        self.assertIn('新的 request_id', status['status_note'])
        self.submit.assert_not_called()
        self.assertEqual(self.bridge.record('old').read_bytes(), original)

    def test_missing_auth_and_uncertain_do_not_offer_resubmission(self):
        self.bridge.backend = 'codex'
        for state, present in (('failed', False), ('uncertain', True)):
            self.bridge.save({'request_id': 'old', 'state': state, 'error': '缺少 Codex ChatGPT 登录授权，请先 codex login'})
            with patch('bridge.codex_authorization_status', return_value={'local_oauth_present': present, 'server_verified': False}):
                status = self.bridge.call({'action': 'status', 'request_id': 'old'})
            self.assertNotIn('新的 request_id', status['status_note'])
        self.submit.assert_not_called()

    def test_auth_metadata_does_not_return_tokens(self):
        folder = self.bridge.root / '.codex'
        folder.mkdir()
        path = folder / 'auth.json'
        path.write_text(json.dumps({'tokens': {'access_token': 'fixture-secret-token'}}), encoding='utf-8')
        with patch('bridge.Path.home', return_value=self.bridge.root):
            result = codex_authorization_status()
        self.assertEqual(result, {'local_oauth_present': True, 'server_verified': False})
        self.assertNotIn('fixture-secret-token', json.dumps(result))
        path.write_text('invalid json', encoding='utf-8')
        with patch('bridge.Path.home', return_value=self.bridge.root):
            self.assertFalse(codex_authorization_status()['local_oauth_present'])

    def test_backend_errors_are_classified_without_returning_raw_text(self):
        examples = [(b'HTTP 429: usage_limit_reached secret-value', 'quota'),
                    (b'HTTP 401: unauthorized secret-value', 'authentication'),
                    (b'backend failed mid-generation: content_policy_violation secret-value', 'content_rejected'),
                    (b'stalled: the image backend sent no data for ~90s secret-value', 'timeout'),
                    (b'network error contacting the image backend secret-value', 'network'),
                    (b'no image returned. events seen: response.completed secret-value', 'no_image'),
                    (b'HTTP 503: secret-value', 'server_error'),
                    (b'ssl.SSLEOFError: secret-value', 'network'),
                    (b'http.client.IncompleteRead: secret-value', 'network'),
                    (b'HTTP 400: invalid_image secret-value', 'invalid_request'),
                    (b'backend failed mid-generation: unspecified secret-value', 'backend_failed'),
                    (b'unknown response secret-value', 'unknown')]
        for raw, expected in examples:
            code, message = backend_failure(raw)
            self.assertEqual(code, expected)
            self.assertNotIn('secret-value', message)

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.bridge = Bridge({"data_dir": self.temp.name, "image_use": "upstream", "chrome_use": "chrome"})
        self.submit = patch.object(self.bridge.pool, "submit").start()

    def tearDown(self):
        patch.stopall()
        self.bridge.pool.shutdown()
        self.temp.cleanup()

    def test_one_tool_and_no_arbitrary_browser_commands(self):
        self.assertEqual(len(rpc(self.bridge, {"method": "tools/list"})["tools"]), 1)
        result = rpc(self.bridge, {"method": "tools/call", "params": {"name": "eval", "arguments": {}}})
        self.assertTrue(result["isError"])
        with self.assertRaises(ValueError):
            self.bridge.call({"action": "generate", "request_id": "x", "prompt": "猫", "script": "evil"})

    def test_request_id_prevents_duplicate_and_changed_resubmission(self):
        args = {"action": "generate", "request_id": "a", "prompt": "猫"}
        self.bridge.call(args)
        self.bridge.call(args)
        self.assertEqual(self.submit.call_count, 1)
        with self.assertRaises(ValueError):
            self.bridge.call(dict(args, prompt="狗"))

    def test_edit_requires_image_and_blocks_paths_urls(self):
        with self.assertRaises(ValueError):
            self.bridge.call({"action": "edit", "request_id": "x", "prompt": "改颜色"})
        for name in ("../secrets.png", "https://a/b.png", "C:\\a.png", "folder/image.png"):
            with self.assertRaises(ValueError):
                self.bridge.reference(name)

    def test_reference_and_parent_are_frozen_for_repeated_edits(self):
        inbox = self.bridge.root / "inbox" / "input.png"
        inbox.write_bytes(PNG)
        self.bridge.call({"action": "edit", "request_id": "first", "prompt": "换背景", "references": ["input.png"]})
        self.assertEqual((self.bridge.root / "first" / "ref-0.png").read_bytes(), PNG)
        inbox.write_bytes(b"changed")
        parent = self.bridge.read("first")
        parent.update(state="succeeded")
        (self.bridge.root / "first.png").write_bytes(PNG)
        self.bridge.save(parent)
        result = self.bridge.call({"action": "edit", "request_id": "second", "prompt": "只改眼睛", "parent_id": "first"})
        self.assertEqual(result["parent_id"], "first")
        self.assertEqual((self.bridge.root / "second" / "ref-0.png").read_bytes(), PNG)

    def test_command_cannot_inject_flags_or_select_codex(self):
        command = self.bridge.command("a", "--backend codex", [])
        self.assertEqual(command[-2:], ["--", "--backend codex" + VISUAL_STRUCTURE])
        self.assertEqual(command[command.index("--backend") + 1], "web")
        self.assertEqual(command[command.index("--profile") + 1], "relay")
        self.assertIn("--no-style", command)
        self.assertIn("--keep-conversation", command)

    def test_codex_is_fixed_by_admin_and_has_no_browser_commands(self):
        self.bridge.backend = "codex"
        command = self.bridge.command("a", "--backend web", [Path("reference.png")])
        self.assertEqual(command[command.index("--backend") + 1], "codex")
        self.assertNotIn("--profile", command)
        self.assertNotIn("--session", command)
        self.assertIn("--ref", command)
        self.assertEqual(command[-2], '--')
        self.assertTrue(command[-1].startswith('--backend web' + VISUAL_STRUCTURE))
        self.assertEqual(command[command.index('--background') + 1], 'opaque')
        self.assertEqual(command[command.index('--model') + 1], 'gpt-6.1-sol')

    def test_queue_capacity_and_rejected_nonimage(self):
        for n in range(3):
            self.bridge.call({"action": "generate", "request_id": str(n), "prompt": "猫"})
        with self.assertRaises(ValueError):
            self.bridge.call({"action": "generate", "request_id": "four", "prompt": "猫"})
        (self.bridge.root / "inbox" / "evil.png").write_bytes(b"<html>not an image</html>")
        with self.assertRaises(ValueError):
            self.bridge.reference("evil.png")

    def test_missing_relay_fails_before_generation(self):
        source, chrome = self.bridge.root / "source", self.bridge.root / "chrome"
        source.write_bytes(b"source")
        chrome.write_bytes(b"program")
        self.bridge.config.update(image_use=str(source), chrome_use=str(chrome),
                                  image_use_sha256=hashlib.sha256(source.read_bytes()).hexdigest(),
                                  chrome_use_sha256=hashlib.sha256(chrome.read_bytes()).hexdigest())
        args = {"action": "generate", "request_id": "relay", "prompt": "猫"}
        job = self.bridge.call(args)
        with patch("bridge.subprocess.run") as process:
            process.return_value.returncode = 0
            process.return_value.stdout = b'{"data":{"relay":false}}'
            self.bridge.run(job, "猫", [])
            self.assertEqual(process.call_count, 1)
        self.assertEqual(self.bridge.read("relay")["state"], "failed")
        self.bridge.call(args)
        self.assertEqual(self.submit.call_count, 1)

    def test_post_submission_failure_is_uncertain_not_retried(self):
        source, chrome = self.bridge.root / "source", self.bridge.root / "chrome"
        source.write_bytes(b"source")
        chrome.write_bytes(b"program")
        self.bridge.config.update(image_use=str(source), chrome_use=str(chrome),
                                  image_use_sha256=hashlib.sha256(source.read_bytes()).hexdigest(),
                                  chrome_use_sha256=hashlib.sha256(chrome.read_bytes()).hexdigest())
        job = self.bridge.call({"action": "generate", "request_id": "timeout", "prompt": "猫"})
        from types import SimpleNamespace
        with patch("bridge.subprocess.run", side_effect=[
            SimpleNamespace(returncode=0, stdout=b'{"data":{"relay":true}}'),
            SimpleNamespace(returncode=1),
        ]) as process:
            self.bridge.run(job, "猫", [])
            self.assertEqual(process.call_count, 2)
        self.assertEqual(self.bridge.read("timeout")["state"], "uncertain")
        self.assertFalse(self.bridge.read("timeout")["auto_retry"])

    def test_status_reader_cannot_overlap_atomic_replace(self):
        """暂停状态读取，确认Windows原子替换必须等待该文件读取结束。"""
        import threading
        self.bridge.save({'request_id': 'race', 'state': 'queued'})
        entered, release, written = threading.Event(), threading.Event(), threading.Event()
        original = Path.read_text
        def blocked(path, *args, **kwargs):
            entered.set()
            release.wait(2)
            return original(path, *args, **kwargs)
        def writer():
            self.bridge.save({'request_id': 'race', 'state': 'running'})
            written.set()
        with patch.object(Path, 'read_text', blocked):
            reader = threading.Thread(target=self.bridge.read, args=('race',))
            reader.start()
            self.assertTrue(entered.wait(1))
            worker = threading.Thread(target=writer)
            worker.start()
            try:
                self.assertFalse(written.wait(0.05), '写入在读取结束前替换了文件')
            finally:
                release.set()
                reader.join(2)
                worker.join(2)
        self.assertTrue(written.is_set())
        self.assertEqual(self.bridge.read('race')['state'], 'running')


if __name__ == "__main__":
    unittest.main()

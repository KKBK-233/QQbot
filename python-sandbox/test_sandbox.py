"""在实际WSL命名空间中验证可用性、隔离和资源耗尽后的恢复。"""

import asyncio
import json
import os
import pwd
from pathlib import Path
import unittest
from unittest.mock import patch, AsyncMock
import runner

from runner import run_python, validate, CGROUP


class InputTests(unittest.TestCase):
    """在创建进程前拦截不合规输入。"""

    def test_paths_and_oversize(self):
        for name in ('../secret', '/etc/passwd', 'a/b', '..', '.hidden', 'a\\b'):
            with self.subTest(name=name), self.assertRaises(ValueError):
                validate('print(1)', json.dumps({name: 'x'}))
        for code, files in [('', '{}'), ('a' * 16385, '{}'), ('x', '[]'),
                            ('x', '{"a": 1}'), ('x', json.dumps({'a': '中' * 30000}))]:
            with self.subTest(code_length=len(code)), self.assertRaises(ValueError):
                validate(code, files)


class IsolationTests(unittest.IsolatedAsyncioTestCase):
    """全部执行真实沙箱，不用模型或QQ消息。"""

    async def test_csv_and_json_files(self):
        code = """import csv, json, statistics
from pathlib import Path
rows = list(csv.DictReader(Path('data.csv').open()))
answer = statistics.mean(float(r['value']) for r in rows)
Path('output/result.json').write_text(json.dumps({'mean': answer}))
print(answer)
"""
        result = await run_python(code, json.dumps({'data.csv': 'value\n2\n4\n6\n'}))
        self.assertTrue(result['success'], result)
        self.assertEqual(result['stdout'], '4.0\n')
        self.assertEqual(json.loads(result['files']['result.json']), {'mean': 4.0})

    async def test_host_files_and_environment_hidden(self):
        # 临时放入无敏感信息的诱饵，证明宿主已存在的文件在沙箱里不可见。
        marker = Path('/home/qqbot/qqbot-sandbox-canary')
        marker.write_text('not-a-secret')
        try:
            with patch.dict(os.environ, {'QQBOT_SANDBOX_CANARY': 'not-a-secret'}):
                result = await run_python("""from pathlib import Path
import os
print([Path(p).exists() for p in ['/home/qqbot/qqbot-sandbox-canary', '/mnt/c', '/mnt/d', '/proc', '/etc/passwd', '/run']])
print(os.environ.get('QQBOT_SANDBOX_CANARY'))
""")
            self.assertTrue(result['success'], result)
            self.assertEqual(result['stdout'], '[False, False, False, False, False, False]\nNone\n')
        finally:
            marker.unlink()

    async def assert_blocked(self, code):
        result = await run_python(code)
        self.assertFalse(result['success'], result)
        self.assertIn('PermissionError', result.get('error', ''), result)

    async def test_network_socket_blocked(self):
        await self.assert_blocked('import socket; socket.socket()')

    async def test_fork_blocked(self):
        await self.assert_blocked('import os; os.fork()')

    async def test_exec_blocked(self):
        await self.assert_blocked("import os; os.execv('/usr/bin/python3.12', ['python3.12', '-c', 'print(1)'])")

    async def test_subprocess_blocked(self):
        await self.assert_blocked("import subprocess; subprocess.run(['/usr/bin/python3.12', '-c', 'print(1)'])")

    async def test_threads_blocked(self):
        result = await run_python('import threading; threading.Thread(target=lambda: None).start()')
        self.assertFalse(result['success'], result)
        self.assertIn('thread', result['error'])

    async def test_syscall_bypass_blocked(self):
        # 直接调用本机x86_64的socket系统调用，验证不是只屏蔽Python模块。
        result = await run_python("import ctypes; c=ctypes.CDLL(None, use_errno=True); print(c.syscall(41, 2, 1, 0), ctypes.get_errno())")
        self.assertTrue(result['success'], result)
        self.assertEqual(result['stdout'], '-1 1\n')

    async def test_runtime_is_readonly(self):
        result = await run_python("open('/usr/bin/python3.12', 'wb')")
        self.assertFalse(result['success'], result)
        self.assertIn('Read-only', result['error'])
        result = await run_python("open('/new-file', 'w')")
        self.assertFalse(result['success'], result)
        self.assertIn('Read-only', result['error'])

    async def test_resource_limits_cannot_be_raised(self):
        result = await run_python('import resource; resource.setrlimit(resource.RLIMIT_CPU, (999, 999))')
        self.assertFalse(result['success'], result)
        self.assertIn('not allowed to raise', result['error'])

    async def test_disk_limit(self):
        result = await run_python("block=b'x'*1048576\nfor i in range(24):\n    open(str(i), 'wb').write(block)")
        self.assertFalse(result['success'], result)
        self.assertIn('No space left', result['error'])

    async def test_output_file_limit(self):
        result = await run_python("open('output/large.txt', 'w').write('x'*17000); open('output/small.txt', 'w').write('完成')")
        self.assertTrue(result['success'], result)
        self.assertEqual(result['files'], {'small.txt': '完成'})
        self.assertIn('large.txt', result['skipped_files'])

    async def test_standard_library_processing(self):
        result = await run_python("import sqlite3, decimal, hashlib, zipfile; print(sqlite3.connect(':memory:').execute('select 1').fetchone())")
        self.assertTrue(result['success'], result)
        self.assertEqual(result['stdout'], '(1,)\n')

    async def test_memory_limit(self):
        result = await run_python('x = bytearray(300 * 1024 * 1024)')
        self.assertFalse(result['success'], result)
        self.assertIn('MemoryError', result['error'])

    async def test_cpu_timeout_and_next_run(self):
        result = await run_python('while True: pass')
        self.assertFalse(result['success'], result)
        result = await run_python('print(7)')
        self.assertTrue(result['success'], result)

    async def test_wall_timeout(self):
        result = await run_python('import time; time.sleep(30)')
        self.assertFalse(result['success'], result)
        self.assertIn('6秒', result['error'])

    async def test_pipe_flood_stopped(self):
        result = await run_python("import os; x=b'x'*4096\nwhile True: os.write(1, x)")
        self.assertFalse(result['success'], result)
        self.assertIn('64 KiB', result['error'])

    async def test_temporary_files_do_not_survive(self):
        first = await run_python("open('only-this-run', 'w').write('x')")
        self.assertTrue(first['success'], first)
        second = await run_python("from pathlib import Path; print(Path('only-this-run').exists())")
        self.assertEqual(second['stdout'], 'False\n')

    async def test_output_symlinks_skipped(self):
        result = await run_python("import os; os.symlink('/worker.py', 'output/link'); os.mkfifo('output/fifo')")
        # mkfifo本身没有被列入允许的系统调用；使用软链接单独检验收集路径。
        self.assertFalse(result['success'], result)
        result = await run_python("import os; os.symlink('/worker.py', 'output/link')")
        self.assertTrue(result['success'], result)
        self.assertEqual(result['files'], {})
        self.assertIn('link', result['skipped_files'])

    async def test_concurrency_and_cancellation(self):
        task = asyncio.create_task(run_python('import time; time.sleep(30)'))
        await asyncio.sleep(0.2)
        try:
            other = await run_python('print(1)')
            self.assertFalse(other['success'], other)
            self.assertIn('其他任务', other['error'])
        finally:
            task.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await task
        result = await run_python('print(2)')
        self.assertTrue(result['success'], result)

    async def test_no_fallback_without_isolation(self):
        with patch('runner.command', side_effect=RuntimeError('缺少隔离组件')):
            result = await run_python('print(1)')
        self.assertEqual(result, {'success': False, 'error': '缺少隔离组件'})

    async def test_cgroup_limits_and_dedicated_user(self):
        expected = {'memory.max': '402653184', 'memory.swap.max': '0', 'pids.max': '8', 'cpu.max': '25000 100000'}
        for name, value in expected.items():
            self.assertEqual((CGROUP / name).read_text().strip(), value)
        result = await run_python('import os; print(os.getuid())')
        self.assertTrue(result['success'], result)
        self.assertEqual(int(result['stdout']), pwd.getpwnam('qqbot-sandbox').pw_uid)
        self.assertEqual((CGROUP / 'cgroup.procs').read_text().strip(), '')

    async def test_untrusted_metrics_ignored(self):
        result = await run_python("import os; os.write(1, b'{\"success\":true,\"usage\":{\"cpu_ms\":999999999}}'); os._exit(0)")
        self.assertTrue(result['success'], result)
        self.assertLess(result['usage']['cpu_ms'], 1000)
        self.assertGreater(result['usage']['cpu_ms'], 0)

    async def test_model_output_budget(self):
        result = await run_python("print('x'*5000)")
        self.assertFalse(result['success'], result)
        self.assertLessEqual(len(json.dumps(result, ensure_ascii=False).encode()), 4096)

    async def test_directory_flood_and_cleanup(self):
        result = await run_python("i=0\nwhile True:\n    open(str(i), 'w').close()\n    i+=1")
        self.assertFalse(result['success'], result)
        self.assertEqual((CGROUP / 'cgroup.procs').read_text().strip(), '')
        result = await run_python('print(1)')
        self.assertTrue(result['success'], result)

    async def test_cleanup_failure_stops_next_execution(self):
        try:
            with patch('runner.kill_sandbox', new=AsyncMock(side_effect=[None, RuntimeError('模拟清理失败')])):
                result = await run_python('print(1)')
            self.assertFalse(result['success'], result)
            self.assertIn('不能确认', result['notice'])
            other = await run_python('print(2)')
            self.assertFalse(other['success'], other)
            self.assertIn('已熔断', other['error'])
        finally:
            runner.BROKEN = False


if __name__ == '__main__':
    unittest.main()

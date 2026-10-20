"""使用真实 Linux 命名空间核验文件、网络隔离及取消后的子进程回收。"""
from pathlib import Path
from tempfile import TemporaryDirectory
import os
import sys
import time
import unittest

from dev_worker.config import WorkerConfig
from dev_worker.process import Stopped,run_process
from dev_worker.workspace import sandbox_command

@unittest.skipUnless(sys.platform=='linux' and Path('/usr/bin/bwrap').exists(),'需要 WSL/Linux bwrap')
class SandboxTests(unittest.TestCase):
    def setUp(self):
        self.temp=TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root=Path(self.temp.name)
        self.cfg=WorkerConfig(root=self.root/'worker',source_repo=self.root/'source')
        self.folder=self.cfg.root/'task'
        for name in ('ws','venv','agent-home'):
            (self.folder/name).mkdir(parents=True)
        self.cfg.base_venv.mkdir(parents=True)

    def run_code(self,code,timeout=5):
        return run_process(sandbox_command(self.cfg,self.folder,['/usr/bin/python3','-c',code]),
            cwd=self.folder/'ws',env={'PATH':'/usr/bin:/bin'},stdout=self.folder/'out',stderr=self.folder/'err',timeout=timeout)

    def test_hidden_files_and_network(self):
        secret=self.root/'outside.txt'; secret.write_text('fixture private')
        code=("from pathlib import Path; import socket; "
              f"assert not Path({str(secret)!r}).exists(); "
              "assert not Path('/home/qqbot/QQbot').exists(); "
              "assert not Path('/mnt/d/code/web/QQbot').exists(); "
              "s=socket.socket(); s.settimeout(0.2); "
              "assert s.connect_ex(('127.0.0.1',8001)) != 0; "
              "Path('allowed.txt').write_text('ok')")
        result=self.run_code(code)
        self.assertEqual(result.exit_code,0,(self.folder/'err').read_text())
        self.assertEqual((self.folder/'ws/allowed.txt').read_text(),'ok')

    def test_timeout_removes_namespace_descendants(self):
        child="import time; from pathlib import Path; time.sleep(1.5); Path('leaked.txt').write_text('leak')"
        code=f"import subprocess,time; subprocess.Popen(['/usr/bin/python3','-c',{child!r}]); time.sleep(20)"
        with self.assertRaises(Stopped) as error:
            self.run_code(code,timeout=0.4)
        self.assertEqual(error.exception.reason,'timeout')
        time.sleep(1.8)
        self.assertFalse((self.folder/'ws/leaked.txt').exists())

    def test_workspace_venv_mount_writes_only_task_environment(self):
        target=self.folder/'ws/.dev-worker-venv/fixture.txt'
        base=self.cfg.base_venv/'base.txt'; base.write_text('baseline')
        code=("from pathlib import Path; "
              f"Path({str(target)!r}).write_text('task dependency'); "
              f"p=Path({str(base)!r}); assert p.read_text()=='baseline'; "
              "\ntry:\n p.write_text('modified')\nexcept OSError:\n pass\nelse:\n raise AssertionError('base venv writable')")
        result=self.run_code(code)
        self.assertEqual(result.exit_code,0,(self.folder/'err').read_text())
        self.assertEqual((self.folder/'venv/fixture.txt').read_text(),'task dependency')
        self.assertEqual(base.read_text(),'baseline')

if __name__=='__main__': unittest.main()

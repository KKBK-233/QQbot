"""开发 worker 的状态、隔离命令、真实 Git 和进程生命周期回归。"""
from dataclasses import replace
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch
import json
import os
import subprocess
import sys
import threading
import time
import unittest

from dev_worker.config import WorkerConfig
from dev_worker.process import Stopped,run_process
from dev_worker.store import Store,atomic_json,exclusive_lock
from dev_worker.worker import Worker,event_summary,outcome
from dev_worker.workspace import agent_env,sandbox_command,prepare_clone,resolve_commit,visible_venv

class WorkerTests(unittest.TestCase):
    def setUp(self):
        self.temp=TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root=Path(self.temp.name)
        self.source=self.root/'source'; self.source.mkdir()
        self.git('init','-q')
        self.git('config','user.name','Worker Test')
        self.git('config','user.email','worker@localhost')
        (self.source/'original.txt').write_text('baseline\n')
        self.git('add','.'); self.git('commit','-qm','baseline')
        self.cfg=WorkerConfig(root=self.root/'worker',source_repo=self.source,base_ref='HEAD',
            use_sandbox=False,agent_path=os.environ['PATH'],prepare_timeout_sec=15,
            agent_timeout_sec=5,test_timeout_sec=5)

    def git(self,*args):
        return subprocess.check_output(['git','-C',str(self.source),*args],stderr=subprocess.PIPE)

    def test_config_strict_boolean_and_directory_validation(self):
        with self.assertRaises(ValueError): WorkerConfig.from_env({'KKBK_DEV_WORKER_SANDBOX':'typo'})
        with self.assertRaises(ValueError): WorkerConfig.from_env({'KKBK_DEV_WORKER_AGENT_TIMEOUT':'0'})
        with self.assertRaises(ValueError): replace(self.cfg,root=self.source/'dev').validate()
        with self.assertRaises(ValueError): replace(self.cfg,root=Path('/home/qqbot/QQbot/dev')).validate()
        self.cfg.validate()

    def test_submit_authorization_and_commit_pinning(self):
        worker=Worker(self.cfg)
        with self.assertRaises(PermissionError): worker.submit('stranger','change code')
        task=worker.submit('1020050112','change code')
        before=task['spec']['base_commit']
        (self.source/'next.txt').write_text('next')
        self.git('add','.'); self.git('commit','-qm','next')
        self.assertNotEqual(before,self.git('rev-parse','HEAD').decode().strip())
        self.assertEqual(worker.store.get(task['id'])['spec']['base_commit'],before)
        self.assertEqual(worker.store.get(task['id'])['status'],'queued')

    def test_silent_tool_has_full_timeout_and_event_delivery_margin(self):
        self.assertEqual(self.cfg.effective_agent_idle_timeout_sec,930)
        self.assertEqual(replace(self.cfg,bash_timeout_ms=900_001).effective_agent_idle_timeout_sec,931)
        self.assertEqual(replace(self.cfg,agent_idle_timeout_sec=1200).effective_agent_idle_timeout_sec,1200)
        with patch('dev_worker.worker.run_process',wraps=run_process) as execute:
            result,_=self.run_fake()
        self.assertEqual(result['status'],'succeeded')
        agent_call=next(call for call in execute.call_args_list if 'idle_timeout' in call.kwargs)
        self.assertEqual(agent_call.kwargs['idle_timeout'],930)

    def test_local_clone_checks_out_commit_only_referenced_by_remote_tracking_ref(self):
        base=self.git('rev-parse','HEAD').decode().strip()
        (self.source/'original.txt').write_text('remote-only commit\n')
        self.git('commit','-qam','remote-only')
        commit=self.git('rev-parse','HEAD').decode().strip()
        self.git('update-ref','refs/remotes/origin/main',commit)
        self.git('reset','--hard',base)
        self.git('gc','--prune=now')
        self.assertEqual(resolve_commit(self.cfg,'origin/main'),commit)
        folder=self.cfg.root/'fixture'; folder.mkdir(parents=True)
        workspace=prepare_clone(self.cfg,folder,commit,lambda:False)
        cloned=subprocess.check_output(['git','-C',str(workspace),'rev-parse','HEAD']).decode().strip()
        self.assertEqual(cloned,commit)
        self.assertEqual((workspace/'original.txt').read_text(),'remote-only commit\n')

    def test_task_paths_terminal_state_and_cancel(self):
        store=Store(self.cfg.root)
        for name in ['../source','/tmp/any','not-a-task']:
            with self.assertRaises(ValueError): store.directory(name)
        task=store.submit({'task':'test'})
        store.cancel(task['id'])
        self.assertFalse(store.finish(task['id'],'succeeded','override'))
        self.assertEqual(store.get(task['id'])['status'],'cancelled')
        task=store.submit({'task':'test'})
        store.claim(); store.cancel(task['id'])
        store.finish(task['id'],'succeeded','race')
        self.assertEqual(store.get(task['id'])['status'],'cancelled')

    def test_lock_and_interrupted_task_recovery(self):
        store=Store(self.cfg.root)
        task=store.submit({'task':'test'}); store.claim()
        with exclusive_lock(self.cfg.lock_path):
            with self.assertRaises(OSError):
                with exclusive_lock(self.cfg.lock_path): pass
            self.assertEqual(store.recover(),1)
        self.assertEqual(store.get(task['id'])['reason'],'worker_interrupted')
        self.assertTrue((store.directory(task['id'])/'summary.json').is_file())

    def test_atomic_json_and_event_protocol(self):
        target=self.root/'result.json'
        atomic_json(target,{'status':'ok'})
        self.assertEqual(json.loads(target.read_text())['status'],'ok')
        self.assertFalse(list(self.root.glob('.write-*')))
        events=self.root/'events.jsonl'
        events.write_text('\n'.join(['not json',json.dumps({'type':'tool_call','tool':'bash'}),
            json.dumps({'type':'status','usage':{'inputTokens':123}}),
            json.dumps({'type':'final','text':'completed'})]))
        data=event_summary(events)
        self.assertEqual(data['tool_calls'],{'bash':1})
        self.assertEqual(data['last_reported_usage'],{'inputTokens':123})
        self.assertEqual(outcome(1,4,0,data),('failed','agent_failed'))
        self.assertEqual(outcome(0,0,None,data),('failed','no_changes'))
        self.assertEqual(outcome(0,4,1,data),('failed','tests_failed'))
        self.assertEqual(outcome(0,4,0,data),('succeeded','verified_patch'))
        self.assertEqual(outcome(0,4,0,{'event_errors':['failure']}),('failed','agent_event_error'))

    def test_environment_and_sandbox_do_not_expose_ledger_or_production(self):
        folder=self.cfg.root/'tasks/example'
        env=agent_env(self.cfg,folder)
        self.assertNotIn('DEEPSEEK_API_KEY',env)
        self.assertNotIn('OPENAI_API_KEY',env)
        cfg=replace(self.cfg,use_sandbox=True)
        with patch('dev_worker.workspace.Path.exists',return_value=True),patch('dev_worker.workspace.Path.is_file',return_value=True):
            command=sandbox_command(cfg,folder,['agent'],agent=True)
            test=sandbox_command(cfg,folder,['python'],agent=False)
        self.assertIn('--unshare-pid',command)
        self.assertIn('--unshare-net',test)
        self.assertNotIn('--unshare-net',command)
        self.assertNotIn(str(folder),command)
        self.assertNotIn(str(self.source),command)
        self.assertNotIn(str(self.cfg.root/'worker.sqlite3'),command)
        self.assertEqual(agent_env(cfg,folder)['VIRTUAL_ENV'],str(visible_venv(cfg,folder)))
        self.assertEqual(visible_venv(cfg,folder),folder/'ws/.dev-worker-venv')
        self.assertIn(str(visible_venv(cfg,folder)),command)
        self.assertIn('/mnt',command)
        key_index=command.index(str(self.cfg.dsh_home/'.env'))
        self.assertEqual(command[key_index-1],'--ro-bind')

    def run_fake(self,*,agent_exit=0,tests_pass=True,change=True,scope='code_change'):
        worker=Worker(self.cfg)
        task=worker.submit('1020050112','add a file',scope=scope)
        # 源目录中的未提交凭据不允许复制进任务。
        (self.source/'.env').write_text('private=fixture')
        folder=worker.store.directory(task['id'])
        code=('from pathlib import Path; import json,sys; '
              + ("Path('new.txt').write_text('new file'); " if change else '')
              + "print(json.dumps({'type':'final','text':'done'})); "
              + f'sys.exit({agent_exit})')
        def prepared(*args):
            (folder/'venv').mkdir()
            (folder/'agent-home').mkdir()
        with patch('dev_worker.worker.prepare_environment',side_effect=prepared),\
             patch('dev_worker.worker.agent_command',return_value=[sys.executable,'-c',code]),\
             patch('dev_worker.worker.test_command',return_value=[sys.executable,'-c',
                "from pathlib import Path; assert not Path('.env').exists(); "+('assert True' if tests_pass else 'assert False')]):
            result=worker.run_once()
        self.assertEqual(self.git('status','--porcelain').decode().strip(),'?? .env')
        return result,folder

    def test_end_to_end_patch_contains_new_file_and_source_untouched(self):
        result,folder=self.run_fake()
        self.assertEqual(result['status'],'succeeded',result)
        patch_text=(folder/'patch.diff').read_text()
        self.assertIn('new file mode',patch_text)
        self.assertIn('new.txt',patch_text)
        self.assertEqual(result['result']['patch']['files'],['new.txt'])
        self.assertFalse((folder/'ws/.env').exists())
        self.assertFalse(subprocess.check_output(['git','-C',str(folder/'ws'),'remote']).strip())

    def test_independent_test_failure_is_not_success(self):
        result,_=self.run_fake(tests_pass=False)
        self.assertEqual((result['status'],result['reason']),('failed','tests_failed'))

    def test_final_event_does_not_override_failed_agent(self):
        result,_=self.run_fake(agent_exit=1)
        self.assertEqual(result['reason'],'agent_failed')

    def test_empty_patch_is_reported(self):
        result,_=self.run_fake(change=False)
        self.assertEqual(result['reason'],'no_changes')

    def test_persistent_workflow_task_rejects_out_of_scope_patch(self):
        result,_=self.run_fake(scope='persistent_workflow')
        self.assertEqual(result['status'],'failed')
        self.assertIn('workflow_scope_violation',result['result']['error'])
        self.assertIsNone(result['result']['test_exit'])

    def test_cli_reports_invalid_id_as_json(self):
        from contextlib import redirect_stderr
        import io
        from dev_worker.__main__ import main
        output=io.StringIO()
        with patch('dev_worker.__main__.WorkerConfig.from_env',return_value=self.cfg),redirect_stderr(output):
            self.assertEqual(main(['status','../escape']),1)
        self.assertEqual(json.loads(output.getvalue())['error'],'ValueError')

    def test_cli_run_failure_has_nonzero_exit(self):
        from contextlib import redirect_stdout
        import io
        from dev_worker.__main__ import main
        config=replace(self.cfg,use_sandbox=True)
        output=io.StringIO()
        with patch('dev_worker.__main__.WorkerConfig.from_env',return_value=config),\
             patch('dev_worker.__main__.Worker') as worker,\
             patch('dev_worker.__main__.sys.platform','linux'),redirect_stdout(output):
            worker.return_value.run_once.return_value={'status':'failed','reason':'agent_failed'}
            self.assertEqual(main(['run','--once']),2)
        self.assertFalse(json.loads(output.getvalue())['success'])

class ProcessTests(unittest.TestCase):
    def test_no_progress_has_an_explicit_end_reason(self):
        with TemporaryDirectory() as name:
            folder=Path(name)
            with self.assertRaises(Stopped) as error:
                run_process([sys.executable,'-c','import time; time.sleep(10)'],cwd=folder,
                    env=dict(os.environ),stdout=folder/'out',stderr=folder/'err',timeout=3,idle_timeout=0.2)
            self.assertEqual(error.exception.reason,'no_progress')

    def test_timeout_cancel_and_output_limit(self):
        with TemporaryDirectory() as name:
            folder=Path(name)
            for reason,code,timeout,cancel in [
                ('timeout','import time; time.sleep(10)',0.2,lambda:False),
                ('cancelled','pass',2,lambda:True),
                ('output_limit',"print('x'*10000)",2,lambda:False)]:
                with self.subTest(reason=reason),self.assertRaises(Stopped) as error:
                    run_process([sys.executable,'-c',code],cwd=folder,env=dict(os.environ),
                        stdout=folder/'out',stderr=folder/'err',timeout=timeout,cancel=cancel,max_bytes=1024)
                self.assertEqual(error.exception.reason,reason)

    @unittest.skipIf(os.name=='nt','Linux 进程组验收在 WSL 执行')
    def test_cancel_cleans_child_process_group(self):
        with TemporaryDirectory() as name:
            folder=Path(name)
            pidfile=folder/'child.pid'
            code=("import subprocess,sys,time; from pathlib import Path; "
                  "p=subprocess.Popen([sys.executable,'-c','import time; time.sleep(30)']); "
                  f"Path({str(pidfile)!r}).write_text(str(p.pid)); time.sleep(30)")
            cancel=threading.Event()
            errors=[]
            def execute():
                try: run_process([sys.executable,'-c',code],cwd=folder,env=dict(os.environ),
                    stdout=folder/'out',stderr=folder/'err',timeout=5,cancel=cancel.is_set)
                except Stopped as exc: errors.append(exc.reason)
            thread=threading.Thread(target=execute); thread.start()
            for _ in range(100):
                if pidfile.exists(): break
                time.sleep(0.02)
            self.assertTrue(pidfile.exists())
            pid=int(pidfile.read_text())
            cancel.set(); thread.join(5)
            self.assertFalse(thread.is_alive())
            self.assertEqual(errors,['cancelled'])
            proc=Path('/proc')/str(pid)/'stat'
            if proc.exists(): self.assertEqual(proc.read_text().split()[2],'Z')

if __name__=='__main__':
    unittest.main()

"""开发会话真实账本、Git 副本和进程续跑回归，不调用外部模型。"""
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch
import json
import os
import shutil
import subprocess
import sys
import unittest

from dev_worker.config import WorkerConfig
from dev_worker.coordinator import Coordinator
from dev_worker.worker import Worker
from dev_worker.workspace import agent_command


class SessionTests(unittest.TestCase):
    def setUp(self):
        self.temp = TemporaryDirectory(); self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        source = self.root / 'source'; source.mkdir()
        for args in [('init', '-q'), ('config', 'user.name', 'Fixture'), ('config', 'user.email', 'fixture@localhost')]:
            subprocess.run(['git', '-C', str(source), *args], check=True, capture_output=True)
        (source / 'original.txt').write_text('baseline\n')
        subprocess.run(['git', '-C', str(source), 'add', '.'], check=True, capture_output=True)
        subprocess.run(['git', '-C', str(source), 'commit', '-qm', 'baseline'], check=True, capture_output=True)
        self.cfg = WorkerConfig(root=self.root / 'worker', source_repo=source, base_ref='HEAD',
                                use_sandbox=False, agent_path=os.environ['PATH'],
                                agent_timeout_sec=5, test_timeout_sec=5)
        self.worker = Worker(self.cfg); self.coordinator = Coordinator(self.worker)
        self.origin = dict(platform='qq', session_id='group-session', group_id='1022382380',
                           user_id='1020050112', message_id='submit-one', text='开发一个功能')
        self.calls = []

    def request(self, operation, payload, origin=None):
        return self.coordinator.dispatch(dict(version=1, operation=operation, origin=origin or self.origin, payload=payload))

    def submit(self):
        return self.request('submit', dict(scope='code_change', instructions='新增文件', acceptance='独立测试通过'))

    def message(self, task_id, number=1, text='继续完善'):
        return self.request('message', dict(task_id=task_id, message=text),
                            {**self.origin, 'message_id':f'follow-{number}'})

    def fake_environment(self, cfg, folder, cancelled):
        (folder / 'agent-home').mkdir()
        (folder / 'venv').mkdir()

    def fake_agent(self, cfg, folder, task, session_id=None):
        self.calls.append((folder, task, session_id))
        # 模拟真实进程读取持久会话和上轮修改；没有重建副本的余地。
        code = (
            'from pathlib import Path; import json; '
            + ("assert Path('first.txt').read_text() == 'first'; " if session_id else '')
            + f"assert {session_id!r} in (None, 'session-offline-one'); "
            + ("Path('second.txt').write_text('second'); " if session_id else "Path('first.txt').write_text('first'); ")
            + "print(json.dumps({'type':'session','sessionId':'session-offline-one'})); "
            + "print(json.dumps({'type':'tool_call','tool':'bash'})); "
            + "print(json.dumps({'type':'final','text':'需要协调者补充验收细节'}))"
        )
        return [sys.executable, '-c', code]

    def run_round(self, command=None):
        with patch('dev_worker.worker.prepare_environment', side_effect=self.fake_environment), \
             patch('dev_worker.worker.agent_command', side_effect=command or self.fake_agent), \
             patch('dev_worker.worker.test_command', return_value=[sys.executable, '-c',
                   "from pathlib import Path; assert Path('first.txt').read_text() == 'first'"]):
            return self.worker.run_once()

    def test_queued_inputs_merge_and_concurrent_messages_form_one_next_round(self):
        root = self.submit()['id']
        self.assertEqual(self.message(root)['id'], root)
        self.worker.store.claim()
        with ThreadPoolExecutor(max_workers=4) as pool:
            results = list(pool.map(lambda n:self.message(root, n), range(2, 6)))
        self.assertEqual(len({r['id'] for r in results}), 1)
        self.assertEqual(len(self.worker.store.list()), 2)
        child = results[0]['id']
        self.assertEqual(len(self.worker.store.inputs(child)), 4)
        self.assertEqual(results[-1]['conversation']['root_task_id'], root)
        self.assertEqual(results[-1]['conversation']['status'], 'queued')
        listing=self.request('list',{})
        self.assertEqual(len(listing),1)
        self.assertEqual(listing[0]['id'],child)

    def test_message_deduplication_survives_restart(self):
        root = self.submit()['id']; self.worker.store.claim()
        first = self.message(root)
        self.coordinator = Coordinator(Worker(self.cfg))
        second = self.message(root, text='补试改写的同一条消息')
        self.assertEqual(first['id'], second['id'])
        self.assertEqual(self.worker.store.inputs(first['id']), ['继续完善'])

    def test_retry_after_delivery_does_not_claim_message_is_still_queued(self):
        root=self.submit()['id']; self.run_round()
        first=self.message(root); self.run_round()
        retry=self.message(root)
        self.assertEqual(first['id'],retry['id'])
        self.assertTrue(retry['message_accepted'])
        self.assertFalse(retry['message_queued'])
        self.assertEqual(len(self.worker.store.list()),2)

    def test_resumes_same_workspace_and_session_without_overwriting_old_artifacts(self):
        root = self.submit()['id']
        first = self.run_round(); self.assertEqual(first['status'], 'succeeded', first)
        original = self.worker.store.directory(root) / 'patch.diff'
        saved = original.read_bytes()
        # 模拟压缩功能上线前开工的旧会话：覆盖层里只有 bash 超时。
        overlay = self.worker.store.directory(root) / 'dsh-patch.yml'
        overlay.write_text('- id: bash-sandbox\n  config:\n    timeoutMs: 900000\n')
        child = self.message(root, text='补齐第二步')['id']
        second = self.run_round(); self.assertEqual(second['status'], 'succeeded', second)
        self.assertIn('compaction-basic', overlay.read_text())
        self.assertIn('retainTokens: 32000', overlay.read_text())
        self.assertEqual(second['id'], child)
        self.assertEqual(self.calls[1][0], self.calls[0][0])
        self.assertEqual(self.calls[1][2], 'session-offline-one')
        self.assertIn('补齐第二步', self.calls[1][1])
        self.assertEqual(original.read_bytes(), saved)
        self.assertEqual(second['result']['patch']['files'], ['first.txt', 'second.txt'])
        self.assertFalse((self.worker.store.directory(child) / 'ws').exists())
        view = self.request('status', {'task_id':root})['conversation']
        self.assertEqual(view['current_task_id'], child)
        self.assertEqual(view['agent_summary'], '需要协调者补充验收细节')
        self.assertEqual(view['tool_calls'], {'bash':1})

    def test_cross_group_continuation_reuses_workspace_and_notifies_new_group(self):
        """跨群续聊保持开发会话、旧产物与旧通知，只有新轮次回到新群。"""
        root=self.submit()['id']; self.run_round()
        saved=(self.worker.store.directory(root)/'patch.diff').read_bytes()
        origin={**self.origin,'session_id':'other-group','group_id':'427204073','message_id':'cross-one'}
        payload={'task_id':root,'message':'跨群补齐接线'}
        child=self.request('message',payload,origin)['id']
        result=self.run_round()
        self.assertEqual(result['status'],'succeeded',result)
        self.assertEqual(result['id'],child)
        self.assertEqual(self.calls[1][0],self.calls[0][0])
        self.assertEqual(self.calls[1][2],'session-offline-one')
        self.assertIn('跨群补齐接线',self.calls[1][1])
        self.assertEqual((self.worker.store.directory(root)/'patch.diff').read_bytes(),saved)
        self.worker.store.queue_notifications()
        old=self.worker.store.take_notification(self.origin['session_id'])
        new=self.worker.store.take_notification(origin['session_id'])
        self.assertEqual(old['task_id'],root)
        self.assertEqual(new['task_id'],child)
        self.worker.store.acknowledge_notification(origin['session_id'],new['id'],new['claim_token'],'receipt-cross')
        # 重复补充不能创建新轮次、迁移通知或重发已经发送的回执。
        retry=self.request('message',payload,origin)
        self.assertEqual(retry['id'],child)
        restarted=Coordinator(Worker(self.cfg))
        view=restarted.dispatch(dict(version=1,operation='status',origin=self.origin,payload={'task_id':child}))
        self.assertEqual(view['conversation']['notification_group_id'],'427204073')
        self.worker.store.queue_notifications()
        self.assertIsNone(self.worker.store.take_notification(origin['session_id']))

    def test_queued_cross_group_inputs_route_to_latest_new_message_not_retries(self):
        """合并待执行补充时更新通知位置，重复请求不把通知拉回旧群。"""
        root=self.submit()['id']
        first={**self.origin,'session_id':'first-group','group_id':'427204073','message_id':'cross-first'}
        last={**self.origin,'session_id':'last-group','group_id':'894179464','message_id':'cross-last'}
        payload={'task_id':root,'message':'补充条件'}
        self.assertEqual(self.request('message',payload,first)['id'],root)
        self.request('message',payload,last)
        self.request('message',payload,first)
        self.assertEqual(len(self.worker.store.inputs(root)),2)
        self.assertEqual(self.worker.store.get(root)['spec']['origin'],self.origin)
        self.assertEqual(self.request('status',{'task_id':root},first)['conversation']['notification_group_id'],'894179464')
        self.run_round(); self.worker.store.queue_notifications()
        self.assertIsNone(self.worker.store.take_notification(self.origin['session_id']))
        self.assertIsNone(self.worker.store.take_notification(first['session_id']))
        self.assertEqual(self.worker.store.take_notification(last['session_id'])['task_id'],root)

    def test_message_during_execution_waits_until_next_round(self):
        root = self.submit()['id']
        def agent(cfg, folder, task, session_id=None):
            self.message(root, text='运行中补充')
            return self.fake_agent(cfg, folder, task, session_id)
        first = self.run_round(agent)
        self.assertEqual(first['status'], 'succeeded')
        self.assertNotIn('运行中补充', self.calls[0][1])
        self.assertEqual(self.run_round()['status'], 'succeeded')
        self.assertIn('运行中补充', self.calls[1][1])

    def test_missing_session_does_not_start_a_fresh_agent(self):
        root = self.submit()['id']; self.worker.store.claim()
        self.worker.store.finish(root, 'failed', 'worker_interrupted')
        self.message(root)
        with patch('dev_worker.worker.agent_command') as command:
            result = self.worker.run_once()
        command.assert_not_called()
        self.assertIn('缺少可继续', result['result']['error'])

    def test_restart_keeps_queued_messages_and_saved_session(self):
        root = self.submit()['id']; self.run_round()
        self.message(root, text='重启后的补充')
        self.worker = Worker(self.cfg); self.coordinator = Coordinator(self.worker)
        result = self.run_round()
        self.assertEqual(result['status'], 'succeeded')
        self.assertEqual(self.calls[-1][2], 'session-offline-one')
        self.assertIn('重启后的补充', self.calls[-1][1])

    def test_interrupted_round_can_continue_from_saved_session_without_replay(self):
        root = self.submit()['id']; first=self.run_round()
        # 模拟进程在写终态前退出：会话事件和工作副本已存在，账本仍为运行中。
        with self.worker.store.connect() as db:
            db.execute("UPDATE tasks SET status='running',result='{}' WHERE id=?",(root,))
        self.message(root)
        result = self.run_round()
        self.assertEqual(result['status'], 'succeeded')
        self.assertEqual(self.worker.store.get(root)['reason'], 'worker_interrupted')
        self.assertEqual(self.calls[-1][2], 'session-offline-one')

    def test_workflow_continuation_publishes_without_resetting_workspace(self):
        from dataclasses import replace
        from tests.test_dev_release import SHIPPED, LOADER
        live=self.root/'live-workflows'; shutil.copytree(SHIPPED,live)
        self.cfg=replace(self.cfg,live_workflows=live,live_loader=LOADER)
        self.worker=Worker(self.cfg); self.coordinator=Coordinator(self.worker)
        root=self.request('submit',dict(scope='persistent_workflow',instructions='更新版本',acceptance='加载通过'))['id']
        def agent(cfg,folder,task,session_id=None):
            version='1.2.0' if session_id else '1.1.0'
            code=("from pathlib import Path; import json; "
                  "p=Path('workflows/catalog/conservative-web-research'); "
                  "v=json.loads((p/'1.0.0.json').read_text(encoding='utf-8')); "
                  f"v['version']={version!r}; (p/({version!r}+'.json')).write_text(json.dumps(v)); "
                  "Path('workflows/enabled.json').write_text(json.dumps({'schema_version':1,'enabled':["
                  f"{{'id':'conservative-web-research','version':{version!r},'file':'catalog/conservative-web-research/'+{version!r}+'.json'}}]}})); "
                  "print(json.dumps({'type':'session','sessionId':'session-offline-one'})); "
                  "print(json.dumps({'type':'final','text':'done'}))")
            return [sys.executable,'-c',code]
        with patch('dev_worker.worker.prepare_environment',side_effect=self.fake_environment), \
             patch('dev_worker.worker.agent_command',side_effect=agent), \
             patch('dev_worker.worker.test_command',return_value=[sys.executable,'-c','pass']):
            first=self.worker.run_once()
            self.assertEqual(first['status'],'succeeded',(first,(self.worker.store.directory(root)/'stderr.log').read_text()))
            self.assertEqual(first['result']['release']['state'],'installed',first)
            self.message(root)
            second=self.worker.run_once()
        self.assertEqual(second['result']['release']['state'],'installed',second)
        self.assertTrue((live/'catalog/conservative-web-research/1.1.0.json').exists())
        self.assertTrue((live/'catalog/conservative-web-research/1.2.0.json').exists())
        self.assertIn('1.2.0',(live/'enabled.json').read_text(encoding='utf-8'))

    def test_wrong_resumed_session_stops_before_tests(self):
        root = self.submit()['id']; self.run_round(); self.message(root)
        def wrong(*args, **kwargs):
            return [sys.executable, '-c', "print('{\"type\":\"session\",\"sessionId\":\"session-other\"}')"]
        result = self.run_round(wrong)
        self.assertEqual(result['status'], 'failed')
        self.assertIn('未继续指定会话', result['result']['error'])
        self.assertIsNone(result['result']['test_exit'])

    def test_cancel_covers_active_and_queued_turns(self):
        root = self.submit()['id']; self.worker.store.claim()
        child = self.message(root)['id']
        result = self.request('cancel', {'task_id':root})
        self.assertTrue(self.worker.store.get(root)['cancel_requested'])
        self.assertEqual(self.worker.store.get(child)['status'], 'cancelled')
        self.assertEqual(result['id'], child)

    def test_other_user_cannot_send_follow_up_from_another_group(self):
        root = self.submit()['id']
        with self.assertRaises(PermissionError):
            self.request('message', dict(task_id=root,message='修改'),
                         {**self.origin,'session_id':'other','group_id':'427204073','user_id':'stranger'})
        self.assertEqual(self.worker.store.inputs(root), [])

    def test_input_limit_and_failed_task_progress_are_explicit(self):
        root = self.submit()['id']
        for n in range(8): self.message(root, n)
        with self.assertRaises(ValueError): self.message(root, 9)
        self.worker.store.claim(); self.worker.store.finish(root,'failed','tests_failed',{'error':'fixture'})
        view=self.request('status',{'task_id':root})
        self.assertEqual(view['conversation']['status'],'failed')
        self.assertEqual(view['error'],'fixture')

    def test_headless_command_uses_installed_session_option(self):
        folder=self.worker.store.directory(self.submit()['id'])
        command=agent_command(self.cfg,folder,'继续','session-offline-one')
        self.assertEqual(command[command.index('--session-id')+1], 'session-offline-one')
        self.assertIn('人设',command[-1])
        with self.assertRaises(ValueError): agent_command(self.cfg,folder,'继续','--forged')


if __name__ == '__main__': unittest.main()

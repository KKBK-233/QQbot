"""协调层权限、作用范围、消息去重、恢复与回执竞争回归。"""
from pathlib import Path
from dataclasses import replace
from tempfile import TemporaryDirectory
from unittest.mock import patch
import asyncio
import json
import os
import subprocess
from concurrent.futures import ThreadPoolExecutor
import unittest

from dev_worker.config import WorkerConfig
from dev_worker.coordinator import Coordinator
from dev_worker.service import serve
from dev_worker.worker import Worker


class CoordinatorTests(unittest.TestCase):
    def setUp(self):
        self.temp=TemporaryDirectory(); self.addCleanup(self.temp.cleanup)
        self.root=Path(self.temp.name); source=self.root/'source'; source.mkdir()
        for args in [('init','-q'),('config','user.name','Fixture'),('config','user.email','fixture@localhost')]:
            subprocess.run(['git','-C',str(source),*args],check=True,capture_output=True)
        (source/'code.py').write_text('value=1\n')
        subprocess.run(['git','-C',str(source),'add','.'],check=True,capture_output=True)
        subprocess.run(['git','-C',str(source),'commit','-qm','fixture'],check=True,capture_output=True)
        self.config=WorkerConfig(root=self.root/'worker',source_repo=source,base_ref='HEAD',use_sandbox=False)
        self.worker=Worker(self.config); self.coordinator=Coordinator(self.worker)
        self.origin={'platform':'qq','session_id':'session-one','group_id':'1022382380',
                     'message_id':'message-one','user_id':'1020050112','text':'以后搜索先查官方原文，固化为工作流'}
        self.payload={'scope':'persistent_workflow','instructions':'新工作流优先官方原文','acceptance':'加载校验及原有测试通过'}

    def request(self,operation='submit',origin=None,payload=None):
        return {'version':1,'operation':operation,'origin':origin or self.origin,
                'payload':self.payload if payload is None else payload}

    def test_actual_sender_and_group_are_required(self):
        for changed in ({'user_id':'stranger'},{'platform':'other'},{'group_id':'unconfigured'}):
            with self.subTest(changed=changed),self.assertRaises(PermissionError):
                self.coordinator.dispatch(self.request(origin={**self.origin,**changed}))
        self.assertEqual(self.worker.store.list(),[])

    def test_current_task_and_implicit_teaching_cannot_be_persisted(self):
        for raw in ('这次先查官方原文','以后也这样，但这次不要保存','普通问候'):
            with self.subTest(raw=raw),self.assertRaises(PermissionError):
                self.coordinator.dispatch(self.request(origin={**self.origin,'text':raw}))
        with self.assertRaises(ValueError):
            self.coordinator.dispatch(self.request(payload={**self.payload,'scope':'current_task'}))

    def test_code_development_requires_original_development_request(self):
        payload={**self.payload,'scope':'code_change'}
        with self.assertRaises(PermissionError):
            self.coordinator.dispatch(self.request(origin={**self.origin,'text':'在吗'},payload=payload))
        result=self.coordinator.dispatch(self.request(origin={**self.origin,'text':'修复搜索插件的错误'},payload=payload))
        self.assertEqual(result['scope'],'code_change')

    def test_host_narrows_misclassified_teaching_to_workflow_scope(self):
        result=self.coordinator.dispatch(self.request(
            origin={**self.origin,'text':'以后先搜官方，搜不到搜社区，这个规则记下来，生成候选补丁并验证，不用发布'},
            payload={**self.payload,'scope':'code_change'}))
        self.assertEqual(result['scope'],'persistent_workflow')

    def test_repeated_message_returns_same_pinned_task_after_restart(self):
        first=self.coordinator.dispatch(self.request())
        other=Coordinator(Worker(self.config))
        second=other.dispatch(self.request(payload={**self.payload,'instructions':'补试重新组织的计划'}))
        self.assertEqual(first['id'],second['id'])
        self.assertEqual(len(self.worker.store.list()),1)
        saved=self.worker.store.get(first['id'])['spec']
        self.assertEqual(saved['origin'],self.origin)
        self.assertIn('原始教学',saved['task'])

    def test_concurrent_repeated_submissions_create_one_task(self):
        with ThreadPoolExecutor(max_workers=4) as pool:
            results=list(pool.map(lambda _:self.coordinator.dispatch(self.request()),range(4)))
        self.assertEqual(len({result['id'] for result in results}),1)
        self.assertEqual(len(self.worker.store.list()),1)

    def test_service_refuses_disabled_sandbox(self):
        with self.assertRaises(RuntimeError): asyncio.run(serve(self.config))

    def test_other_session_cannot_query_or_cancel(self):
        task=self.coordinator.dispatch(self.request())
        origin={**self.origin,'session_id':'session-other'}
        for operation in ('status','cancel'):
            with self.assertRaises(PermissionError):
                self.coordinator.dispatch(self.request(operation,origin,{'task_id':task['id']}))
        self.assertEqual(self.coordinator.dispatch(self.request('list',origin,{})),[])

    def test_queued_cancel_and_single_terminal_notification(self):
        task=self.coordinator.dispatch(self.request())
        result=self.coordinator.dispatch(self.request('cancel',payload={'task_id':task['id']}))
        self.assertEqual(result['status'],'cancelled')
        store=self.worker.store
        store.queue_notifications(); store.queue_notifications()
        note=store.take_notification(self.origin['session_id'])
        self.assertIsNotNone(note)
        self.assertIsNone(store.take_notification(self.origin['session_id']))
        # 服务重启也不把发送中状态重置为待发送。
        other=Worker(self.config).store
        other.queue_notifications()
        self.assertIsNone(other.take_notification(self.origin['session_id']))
        with self.assertRaises(PermissionError):
            other.acknowledge_notification('wrong-session',note['id'],note['claim_token'],'receipt-one')
        other.acknowledge_notification(self.origin['session_id'],note['id'],note['claim_token'],'receipt-one')
        other.acknowledge_notification(self.origin['session_id'],note['id'],note['claim_token'],'receipt-one')
        self.assertEqual(other.notification_state(task['id'])['state'],'sent')

    def test_interrupted_task_has_failure_and_completion_outbox(self):
        task=self.coordinator.dispatch(self.request())
        self.worker.store.claim()
        self.assertIsNone(Worker(self.config).run_once())
        self.worker.store.queue_notifications()
        note=self.worker.store.take_notification(self.origin['session_id'])
        self.assertEqual(note['task_id'],task['id'])
        self.assertEqual(self.worker.store.get(task['id'])['reason'],'worker_interrupted')

    def test_delivery_nonce_survives_redacted_wire_response(self):
        task=self.coordinator.dispatch(self.request())
        self.worker.store.cancel(task['id'])
        note=self.coordinator.dispatch(self.request('notifications',payload={'session_id':self.origin['session_id']}))
        wire=json.loads(json.dumps(self.worker.redactor.value(note)))
        self.assertEqual(len(wire['delivery_nonce']),32)
        self.assertNotIn('claim_token',wire)
        ack=self.coordinator.dispatch(self.request('acknowledge',payload={
            'session_id':self.origin['session_id'],'id':wire['id'],
            'delivery_nonce':wire['delivery_nonce'],'receipt':'real-receipt'}))
        self.assertTrue(ack['acknowledged'])
        self.assertEqual(self.worker.store.notification_state(task['id'])['state'],'sent')

    @unittest.skipUnless(os.name=='posix','Unix socket 需要 Linux/WSL')
    def test_real_unix_socket_mode_and_control_without_blocking_worker(self):
        async def scenario():
            ready=asyncio.Event(); shutdown=asyncio.Event()
            # 不执行模型，保持真实 server、协议、SQLite 与权限判断。
            with patch.object(Worker,'run_once',return_value=None):
                service=asyncio.create_task(serve(replace(self.config,use_sandbox=True),ready=ready,shutdown=shutdown))
                try:
                    await asyncio.wait_for(ready.wait(),5)
                    path=self.config.root/'run/control.sock'
                    self.assertEqual(path.stat().st_mode & 0o777,0o600)
                    reader,writer=await asyncio.open_unix_connection(str(path))
                    writer.write((json.dumps(self.request())+'\n').encode()); await writer.drain()
                    response=json.loads(await asyncio.wait_for(reader.readline(),3))
                    writer.close(); await writer.wait_closed()
                    self.assertTrue(response['success'],response)
                    self.assertEqual(response['result']['status'],'queued')
                finally:
                    shutdown.set(); await asyncio.wait_for(service,5)
                self.assertFalse(path.exists())
        asyncio.run(scenario())


if __name__=='__main__': unittest.main()

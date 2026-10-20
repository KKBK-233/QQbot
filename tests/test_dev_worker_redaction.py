"""凭据展示边界回归：覆盖未知前缀、截断、嵌套事件和持久化异常。"""
from contextlib import redirect_stdout
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch
import io
import json
import os
import subprocess
import unittest

from dev_worker.__main__ import main
from dev_worker.config import WorkerConfig
from dev_worker.redaction import MASK,Redactor
from dev_worker.worker import Worker,event_summary


class RedactionTests(unittest.TestCase):
    def test_arbitrary_known_secret_and_common_credential_syntax(self):
        secret='fixture-provider-value-without-token-prefix'
        clean=Redactor([secret]).text('value '+secret)
        self.assertEqual(clean,'value '+MASK)
        for raw in ('Authorization: Bearer fixture-auth-value',
                    'Authorization: Basic Zml4dHVyZTpwYXNz',
                    'API_KEY="fixture space value"',
                    'Cookie: session=fixture-cookie; another=fixture-cookie-two',
                    '{"refresh_token": "fixture-refresh-value"}',
                    'https://fixture-user:fixture-pass@example.invalid/path',
                    'https://example.invalid/?access_token=fixture-query&ok=yes',
                    'github_pat_fixture00000000000000000',
                    'sk-fixture000000000000000',
                    'ghp_fixture000000000000000'):
            with self.subTest(raw=raw):
                clean=Redactor().text(raw)
                self.assertIn(MASK,clean)
                self.assertNotIn('fixture-',clean)
        kind='PRIVATE KEY'
        private=f'-----BEGIN {kind}-----\nfixture-private-data\n-----END {kind}-----'
        self.assertEqual(Redactor().text(private),MASK)
        self.assertEqual(Redactor().text('Cookie: a=one; b=two'), 'Cookie: '+MASK)

    def test_read_only_dotenv_and_environment_values(self):
        with TemporaryDirectory() as name:
            path=Path(name)/'.env'
            content='\ufeffexport DEEPSEEK_API_KEY="fixture provider one" # comment\nOTHER_API_KEY=fixture-two # note\nORDINARY=retain\n'
            path.write_text(content,encoding='utf-8')
            with patch.dict(os.environ,{'GITHUB_TOKEN':'fixture-three'}):
                redact=Redactor.from_env_file(path)
            self.assertEqual(redact.text('fixture provider one / fixture-two / fixture-three'),f'{MASK} / {MASK} / {MASK}')
            self.assertEqual(path.read_text(encoding='utf-8'),content)
            self.assertEqual(redact.text('retain'),'retain')

    def test_nested_values_keep_usage_numbers(self):
        value={'inputTokens':123,'output_tokens':4,'nested':{'api_key':'unrecognized-value'},
               'items':['Bearer fixture-auth'], 'message':'password=fixture-pass'}
        result=Redactor().value(value)
        self.assertEqual(result['inputTokens'],123)
        self.assertEqual(result['output_tokens'],4)
        self.assertEqual(result['nested']['api_key'],MASK)
        self.assertNotIn('fixture',json.dumps(result))

    def test_event_sanitization_happens_before_truncation(self):
        with TemporaryDirectory() as name:
            path=Path(name)/'events.jsonl'
            secret='fixture-long-provider-secret-abcdefghijk'
            events=[{'type':'final','text':secret+'x'*8180},
                    {'type':'error','message':'x'*500+secret},
                    {'type':'status','usage':{'inputTokens':123,'api_key':secret}},
                    {'type':'session','sessionId':secret}]
            raw='\n'.join(json.dumps(event) for event in events)
            path.write_text(raw)
            result=event_summary(path,Redactor([secret]))
            self.assertNotIn('fixture',json.dumps(result))
            self.assertEqual(result['last_reported_usage']['inputTokens'],123)
            self.assertEqual(result['session_id'],MASK)
            self.assertEqual(path.read_text(),raw)

    def test_worker_exception_and_cli_task_projection_are_redacted(self):
        with TemporaryDirectory() as name:
            root=Path(name); source=root/'source'; source.mkdir()
            def git(*args):
                subprocess.run(['git','-C',str(source),*args],check=True,capture_output=True)
            git('init','-q'); git('config','user.name','Fixture'); git('config','user.email','fixture@localhost')
            (source/'code.py').write_text('value=1\n')
            git('add','.'); git('commit','-qm','fixture')
            cfg=WorkerConfig(root=root/'worker',source_repo=source,base_ref='HEAD',use_sandbox=False)
            cfg.dsh_home.mkdir(parents=True)
            secret='fixture-unprefixed-provider-key'
            (cfg.dsh_home/'.env').write_text('DEEPSEEK_API_KEY='+secret)
            worker=Worker(cfg)
            task=worker.submit('1020050112','fixture task '+secret)
            with patch('dev_worker.worker.prepare_clone',side_effect=RuntimeError('x'*500+secret)):
                result=worker.run_once()
            saved=json.loads((worker.store.directory(task['id'])/'summary.json').read_text())
            self.assertNotIn('fixture-unprefixed',json.dumps(saved))
            self.assertNotIn(secret,result['result']['error'])
            # 任务原文仅在私有账本保留供代理执行，CLI 展示另行脱敏。
            self.assertIn(secret,worker.store.get(task['id'])['spec']['task'])
            output=io.StringIO()
            with patch('dev_worker.__main__.WorkerConfig.from_env',return_value=cfg),redirect_stdout(output):
                self.assertEqual(main(['status',task['id']]),0)
            self.assertNotIn(secret,output.getvalue())
            self.assertEqual(json.loads(output.getvalue())['result']['status'],'failed')
            interrupted=worker.submit('1020050112','interrupted task '+secret)
            worker.store.claim()
            self.assertIsNone(worker.run_once())
            snapshot=(worker.store.directory(interrupted['id'])/'summary.json').read_text()
            self.assertNotIn(secret,snapshot)
            self.assertEqual(json.loads(snapshot)['reason'],'worker_interrupted')


if __name__=='__main__': unittest.main()

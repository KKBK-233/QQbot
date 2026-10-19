"""离线验证 OAuth 边界、并发刷新、工具回放与流中断，不消耗模型额度。"""

from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch
from urllib.parse import parse_qs, urlsplit

import asyncio
import importlib
import json
import os
import sys
import tempfile
import time
import unittest

ROOT = Path('/home/qqbot/QQbot/MaiBot')
sys.path.insert(0, str(ROOT))
os.chdir(ROOT)

from src.llm_models.exceptions import ReqAbortException, RespParseException
from src.llm_models.model_client.openai_chatgpt_plan import consume_plan_stream, plan_payload, record_cache_usage

auth = importlib.import_module('src.llm_models.model_client.chatgpt_plan_auth')
provider = importlib.import_module('src.llm_models.model_client.openai_chatgpt_plan')


class FakeStream:
    def __init__(self, events):
        self.events = events
        self.closed = False

    def __aiter__(self):
        return self.iterate()

    async def iterate(self):
        for event in self.events:
            yield event

    async def close(self):
        self.closed = True


class PayloadTests(unittest.TestCase):
    def test_stable_cache_key_and_canonical_tools_without_unsupported_markers(self):
        a = {'type': 'function', 'name': 'a', 'parameters': {'type': 'object', 'properties': {'y': {}, 'x': {}}}}
        b = {'type': 'function', 'name': 'b', 'parameters': {'type': 'object', 'properties': {}}}
        first = plan_payload([{'role': 'user', 'content': '问题'}], [a, b], 'gpt-6.1-sol', {})
        second = plan_payload([{'role': 'user', 'content': '新问题'}], [b, a], 'gpt-6.1-sol', {})
        self.assertEqual(first['prompt_cache_key'], second['prompt_cache_key'])
        self.assertEqual(first['prompt_cache_key'], 'maibot-plan-v2:default')
        self.assertEqual(first['tools'], second['tools'])
        self.assertNotIn('prompt_cache_options', first)
        self.assertNotIn('prompt_cache_breakpoint', json.dumps(first))
        custom = plan_payload([], [], 'gpt-6.1-sol', {'prompt_cache_key': 'admin-chosen'})
        self.assertEqual(custom['prompt_cache_key'], 'admin-chosen')

    def test_full_history_namespace_and_unsupported_fields(self):
        source = [{'role': 'system', 'content': '人设'}, {'role': 'user', 'content': '问题'},
                  {'type': 'function_call', 'call_id': 'call1', 'name': 'lookup', 'arguments': '{}'},
                  {'type': 'function_call_output', 'call_id': 'call1', 'output': '甲乙丙'}]
        tool = {'type': 'function', 'name': 'lookup', 'parameters': {'type': 'object', 'properties': {}}}
        result = plan_payload(source, [tool], 'gpt-6.1-sol', {'temperature': .7, 'max_output_tokens': 10})
        self.assertEqual(result['input'][0]['role'], 'developer')
        self.assertEqual(result['input'][-1]['output'], '甲乙丙')
        self.assertEqual(result['input'][2]['namespace'], 'maibot')
        self.assertEqual(source[0]['role'], 'system')
        self.assertEqual(result['tools'][0]['tools'][0]['strict'], False)
        self.assertTrue(result['stream'])
        self.assertFalse(result['store'])
        self.assertNotIn('max_output_tokens', result)
        self.assertNotIn('temperature', result)

    def test_protected_and_unknown_options_rejected(self):
        for key in ['model', 'input', 'stream', 'store', 'tools', 'unrecognized']:
            with self.assertRaises(ValueError):
                plan_payload([], [], 'gpt-6.1-sol', {key: 'bad'})

    def test_model_effort_validation(self):
        with self.assertRaises(ValueError):
            plan_payload([], [], 'gpt-6.1-sol', {'reasoning': {'effort': 'none'}})

    def test_encrypted_reasoning_and_images_preserved(self):
        items = [{'type': 'reasoning', 'encrypted_content': 'opaque'},
                 {'role': 'user', 'content': [{'type': 'input_image', 'image_url': 'data:image/png;base64,x'}]}]
        result = plan_payload(items, [], 'gpt-6.1-sol', {})
        self.assertEqual(result['input'], items)
        self.assertIn('reasoning.encrypted_content', result['include'])


class CacheUsageTests(unittest.TestCase):
    def test_private_usage_only_and_missing_counters_are_unknown(self):
        request = SimpleNamespace(model_info=SimpleNamespace(model_identifier='gpt-6.1-sol'),
                                  trace_context=SimpleNamespace(task_name='planner', request_type='test',
                                                                session_id='private-session'))
        completed = {'usage': {'input_tokens': 2000, 'input_tokens_details': {'cached_tokens': 1024},
                               'output_tokens': 3}, 'output': [{'text': 'private-chat'}]}
        with tempfile.TemporaryDirectory() as folder:
            with patch.object(provider, 'Path', return_value=Path(folder)):
                record_cache_usage(completed, request)
            target = Path(folder) / 'cache-usage.jsonl'
            text = target.read_text()
            entry = json.loads(text)
            self.assertEqual(entry['cached_tokens'], 1024)
            self.assertIsNone(entry['cache_write_tokens'])
            self.assertNotIn('private-session', text)
            self.assertNotIn('private-chat', text)
            self.assertEqual(target.stat().st_mode & 0o777, 0o600)

    def test_telemetry_write_failure_does_not_fail_completed_model_request(self):
        request = SimpleNamespace(model_info=SimpleNamespace(model_identifier='gpt-6.1-sol'), trace_context=None)
        with patch.object(provider.os, 'open', side_effect=OSError('failed')), self.assertLogs(provider.LOGGER, 'WARNING'):
            record_cache_usage({'usage': {}}, request)


class StreamTests(unittest.IsolatedAsyncioTestCase):
    async def test_done_tool_recovered_and_namespaced_tool_unwrapped(self):
        stream = FakeStream([
            {'type': 'response.output_item.done', 'output_index': 1,
             'item': {'type': 'function_call', 'name': 'maibot.lookup', 'namespace': 'maibot', 'call_id': 'x'}},
            {'type': 'response.output_item.done', 'output_index': 0,
             'item': {'type': 'reasoning', 'encrypted_content': 'opaque'}},
            {'type': 'response.completed', 'response': {'status': 'completed', 'output': []}},
        ])
        result = await consume_plan_stream(stream)
        self.assertEqual(result['output'][1]['name'], 'lookup')
        self.assertEqual(result['output'][0]['encrypted_content'], 'opaque')
        self.assertTrue(stream.closed)

    async def test_no_terminal_event_is_failure(self):
        stream = FakeStream([{'type': 'response.output_text.delta', 'delta': 'partial'}])
        with self.assertRaises(RespParseException):
            await consume_plan_stream(stream)
        self.assertTrue(stream.closed)

    async def test_failed_incomplete_and_error_are_failures(self):
        for kind in ['response.failed', 'response.incomplete', 'error']:
            stream = FakeStream([{'type': kind}])
            with self.assertRaises(RespParseException):
                await consume_plan_stream(stream)
            self.assertTrue(stream.closed)

    async def test_interrupt_closes_stream(self):
        flag = asyncio.Event()
        flag.set()
        stream = FakeStream([{'type': 'response.completed', 'response': {}}])
        with self.assertRaises(ReqAbortException):
            await consume_plan_stream(stream, flag)
        self.assertTrue(stream.closed)

    async def test_unknown_namespace_rejected(self):
        stream = FakeStream([{'type': 'response.completed', 'response': {'status': 'completed', 'output': [
            {'type': 'function_call', 'name': 'lookup', 'namespace': 'foreign'}]}}])
        with self.assertRaises(RespParseException):
            await consume_plan_stream(stream)


class AuthTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.store = auth.AccountStore(Path(self.temp.name) / 'auth/accounts.json')
        self.tokens = {'access_token': 'fake-access', 'refresh_token': 'fake-refresh',
                       'id_token': 'fake-id', 'scope': auth.SCOPES, 'expires_in': 3600}

    def tearDown(self):
        self.temp.cleanup()

    def do_login(self):
        attempt = self.store.begin('http://127.0.0.1:18161/auth/callback')
        with patch.object(auth, 'exchange', return_value=self.tokens), patch.object(auth, 'validate_identity',
                   return_value={'sub': 'subject1', 'email': 'private@example.test'}):
            self.store.complete(attempt, {'state': attempt['state'], 'client_id': 'oaiapp_test', 'code': 'code'})

    def test_new_registration_pkce_stable_host_and_private_file(self):
        a = self.store.begin('http://127.0.0.1:18161/auth/callback')
        b = self.store.begin('http://127.0.0.1:18161/auth/callback')
        qa, qb = parse_qs(urlsplit(a['url']).query), parse_qs(urlsplit(b['url']).query)
        self.assertEqual(qa['ext_agent_host_id'], qb['ext_agent_host_id'])
        self.assertNotEqual(a['state'], b['state'])
        self.assertEqual(qa['client_id'], ['dynamic_agent_client'])
        self.assertEqual(qa['code_challenge_method'], ['S256'])
        self.assertEqual(self.store.path.stat().st_mode & 0o777, 0o600)

    def test_bad_state_or_missing_client_does_not_exchange(self):
        attempt = self.store.begin('http://127.0.0.1:18161/auth/callback')
        with patch.object(auth, 'exchange') as exchange:
            for query in [{'state': 'wrong'}, {'state': attempt['state'], 'code': 'x'}]:
                with self.assertRaises(auth.AuthError):
                    self.store.complete(attempt, query)
            exchange.assert_not_called()

    def test_returning_login_uses_issued_client_and_no_name_hint(self):
        self.do_login()
        key = self.store.public_status()['accounts'][0]['key']
        attempt = self.store.begin('http://127.0.0.1:18161/auth/callback', key)
        query = parse_qs(urlsplit(attempt['url']).query)
        self.assertEqual(query['client_id'], ['oaiapp_test'])
        self.assertNotIn('agent_name_hint', query)
        self.assertEqual(query['id_token_hint'], ['fake-id'])
        self.assertNotIn('access_token', json.dumps(self.store.public_status()))

    def test_concurrent_refresh_rotates_once(self):
        self.do_login()
        with self.store.locked() as data:
            record = data['accounts'][data['active']]
            record['expires_at'] = time.time() - 1
            auth.atomic_save(self.store.path, data)
        replacement = dict(self.tokens, access_token='new-access', refresh_token='new-refresh')
        with patch.object(auth, 'exchange', return_value=replacement) as exchange, patch.object(auth, 'validate_identity',
                        return_value={'sub': 'subject1'}):
            with ThreadPoolExecutor(max_workers=4) as executor:
                tokens = list(executor.map(lambda _: self.store.token(), range(4)))
            self.assertEqual(tokens, ['new-access'] * 4)
            self.assertEqual(exchange.call_count, 1)
        saved = json.loads(self.store.path.read_text())
        self.assertEqual(saved['accounts'][saved['active']]['refresh_token'], 'new-refresh')

    def test_ungranted_scope_cannot_activate(self):
        attempt = self.store.begin('http://127.0.0.1:18161/auth/callback')
        with patch.object(auth, 'exchange', return_value=dict(self.tokens, scope='openid')), patch.object(auth, 'validate_identity', return_value={'sub': 'subject1'}):
            with self.assertRaises(auth.AuthError):
                self.store.complete(attempt, {'state': attempt['state'], 'client_id': 'oaiapp_test', 'code': 'code'})
        self.assertFalse(self.store.public_status()['authenticated'])


if __name__ == '__main__':
    unittest.main()

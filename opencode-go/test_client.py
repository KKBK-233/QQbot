"""用生产SDK和模拟HTTP验证客户端，不读取真实密钥或调用模型。"""
import asyncio
import json
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

import httpx

ROOT = Path('/home/qqbot/QQbot/MaiBot')
os.chdir(ROOT)
sys.path.insert(0, str(ROOT))
from src.config.model_configs import APIProvider, ModelInfo
from src.llm_models.model_client import opencode_go_client as module
from src.llm_models.model_client.base_client import ResponseRequest, RequestTraceContext
from src.llm_models.payload_content.context_item import ContextItemBuilder, RoleType, ContextItemMeta, ContextToolCall, build_portable_output_items
from src.llm_models.payload_content.tool_option import ToolOption
from src.llm_models.exceptions import NetworkConnectionError, ReqAbortException, RespNotOkException, RespParseException


class ClientTests(unittest.IsolatedAsyncioTestCase):
    """覆盖工具Schema、隐私、固定代理、会话、错误和取消边界。"""

    async def asyncSetUp(self):
        self.folder = tempfile.TemporaryDirectory()
        self.key = Path(self.folder.name) / 'key'
        self.key.write_text('oc_sk_FAKE_TEST_ONLY')
        self.key.chmod(0o600)
        self.key_patch = patch.object(module, 'KEY_FILE', self.key)
        self.key_patch.start()
        self.cache_patch = patch.object(module, 'CACHE_USAGE_FILE', Path(self.folder.name) / 'cache-usage.jsonl')
        self.cache_patch.start()
        self.provider = APIProvider(name='OpenCodeGoTest', base_url=module.BASE, client_type='opencode_go', auth_type='none')
        self.client = module.OpenCodeGoClient(self.provider)
        self.model = ModelInfo(name='haiku-test', model_identifier=module.MODEL, api_provider=self.provider.name, send_temperature=False)
        self.request = ResponseRequest(model_info=self.model,
            context_items=[ContextItemBuilder().set_role(RoleType.User).add_text_content('合成代码测试').build()],
            max_tokens=256, trace_context=RequestTraceContext(session_id='fake-session'))
        self.native = {'model': module.MODEL, 'stop_reason': 'end_turn', 'content': [{'type': 'text', 'text': '代码测试通过'}],
                       'usage': {'input_tokens': 10, 'output_tokens': 5, 'cache_read_input_tokens': 20}}
        self.original_http = httpx.AsyncClient

    async def asyncTearDown(self):
        self.key_patch.stop()
        self.cache_patch.stop()
        self.folder.cleanup()

    def mock_client(self, handler):
        """验证固定代理后用无网络运输替换，禁止模拟测试实际连接代理。"""
        def factory(**kwargs):
            self.assertEqual(kwargs.pop('proxy'), 'http://127.0.0.1:2081')
            self.assertFalse(kwargs['trust_env'])
            self.assertFalse(kwargs['follow_redirects'])
            return self.original_http(transport=httpx.MockTransport(handler), **kwargs)
        return patch.object(module.httpx, 'AsyncClient', factory)

    async def test_fixed_proxy_cannot_be_overridden_or_bypassed_by_environment(self):
        """环境中的错误代理和 NO_PROXY 不能让 OpenCode 绕过专用代理。"""
        with patch.dict(os.environ, {'HTTP_PROXY': 'http://invalid.test:1',
                                     'HTTPS_PROXY': 'http://invalid.test:2',
                                     'NO_PROXY': '*'}), \
             self.mock_client(lambda _: httpx.Response(200, json=self.native)):
            response = await self.client.get_response(self.request)
        self.assertEqual(response.content, '代码测试通过')

    async def test_text_headers_and_usage(self):
        def handler(request):
            self.assertEqual(str(request.url), module.BASE + '/messages')
            self.assertEqual(request.headers['x-api-key'], 'oc_sk_FAKE_TEST_ONLY')
            self.assertEqual(request.headers['User-Agent'], 'QQbot-OpenCode-Integration/1.0')
            self.assertTrue(request.headers['x-opencode-session'].startswith('qqbot-'))
            self.assertNotIn('fake-session', request.headers['x-opencode-session'])
            self.assertEqual(json.loads(request.content)['thinking'], {'type': 'disabled'})
            return httpx.Response(200, json=self.native)
        with self.mock_client(handler):
            result = await self.client.get_response(self.request)
        self.assertEqual(result.content, '代码测试通过')
        self.assertEqual(result.usage.prompt_tokens, 30)
        self.assertEqual(result.usage.prompt_cache_hit_tokens, 20)
        self.assertEqual(result.wire_protocol, 'anthropic_messages')

    async def test_real_sdk_tool_schema(self):
        self.request.tool_options = [ToolOption(name='probe', description='代码探针')]
        native = {**self.native, 'stop_reason': 'tool_use', 'content': [{'type': 'tool_use', 'id': 'x', 'name': 'probe', 'input': {}}]}
        def handler(request):
            self.assertEqual(json.loads(request.content)['tools'][0]['name'], 'probe')
            self.assertEqual(json.loads(request.content)['tools'][0]['cache_control'], {'type': 'ephemeral'})
            return httpx.Response(200, json=native)
        with self.mock_client(handler):
            result = await self.client.get_response(self.request)
        self.assertEqual(result.tool_calls[0].call_id, 'x')
        self.assertEqual(result.output_items[0].meta.logical_turn_id, self.request.logical_turn_id)

    async def test_error_does_not_echo_body(self):
        with self.mock_client(lambda _: httpx.Response(403, json={'error': {'message': 'SECRET_BODY'}})):
            with self.assertRaises(RespNotOkException) as error:
                await self.client.get_response(self.request)
        self.assertNotIn('SECRET_BODY', str(error.exception))

    def signed_tool_request(self):
        """构建含已签名识图调用及结果的回合，不执行实际工具。"""
        from src.llm_models.model_client.opencode_go_native import native_item, native_scope
        content = [{'type': 'thinking', 'thinking': '', 'signature': 'FAKE_SIGNATURE'},
                   {'type': 'tool_use', 'id': 'image-call', 'name': 'analyze_task_image', 'input': {'message_id': 'image-1'}}]
        portable = build_portable_output_items(content='', logical_turn_id='image-turn',
            tool_calls=[ContextToolCall.create(call_id='image-call', func_name='analyze_task_image', args={'message_id': 'image-1'})])
        result = ContextItemBuilder().set_role(RoleType.Tool).set_meta(ContextItemMeta.create(logical_turn_id='image-turn')).set_tool_call_id('image-call').add_text_content('真实识图结果保持原样').build()
        return self.request.copy_with(context_items=[*self.request.context_items,
            native_item(content, 'image-turn', native_scope(self.provider)), *portable, result])

    async def test_invalid_signature_retry_preserves_tool_result_and_future_native_turns(self):
        """签名错误最多一次可见投影恢复，后续同一旧回合不重复报错。"""
        request = self.signed_tool_request()
        captured = []
        def handler(http_request):
            captured.append(json.loads(http_request.content))
            if len(captured)==1:
                return httpx.Response(400,json={'error':{'type':'invalid_request_error','message':'Invalid signature in thinking block SECRET_BODY'}})
            return httpx.Response(200,json=self.native)
        with self.mock_client(handler):
            recovered = await self.client.get_response(request)
            await self.client.get_response(request)
        self.assertEqual(len(captured),3)
        self.assertEqual(captured[0]['messages'][1]['content'][0]['type'],'thinking')
        for body in captured[1:]:
            self.assertEqual(body['messages'][1]['content'][0]['type'],'tool_use')
            self.assertEqual(body['messages'][1]['content'][0]['id'],'image-call')
            self.assertEqual(body['messages'][2]['content'][0]['tool_use_id'],'image-call')
            self.assertEqual(body['messages'][2]['content'][0]['content'],'真实识图结果保持原样')
            self.assertEqual(body['thinking'],captured[0]['thinking'])
        self.assertEqual(recovered.request_wire_payload,captured[1])
        self.assertEqual(self.client._portable_turn_ids,{'image-turn'})

    async def test_other_400_does_not_retry_or_mark_history(self):
        """普通参数错误及内容拒绝不能触发签名恢复。"""
        captured=[]
        def handler(request):
            captured.append(request)
            return httpx.Response(400,json={'error':{'message':'invalid max_tokens SECRET_BODY'}})
        with self.mock_client(handler), self.assertRaises(RespNotOkException):
            await self.client.get_response(self.signed_tool_request())
        self.assertEqual(len(captured),1)
        self.assertEqual(self.client._portable_turn_ids,set())

    async def test_signature_recovery_failure_does_not_loop(self):
        """恢复失败立即返回，不再次重试或污染回合缓存。"""
        captured=[]
        def handler(request):
            captured.append(request)
            return httpx.Response(400,json={'error':{'message':'invalid thinking signature SECRET_BODY'}})
        with self.mock_client(handler), self.assertRaises(RespNotOkException):
            await self.client.get_response(self.signed_tool_request())
        self.assertEqual(len(captured),2)
        self.assertEqual(self.client._portable_turn_ids,set())

    async def test_parse_diagnostics_are_filtered(self):
        invalid = {**self.native, 'model': 'SECRET_BODY', 'stop_reason': 'SECRET_BODY',
                   'content': [{'type': 'SECRET_BODY'}], 'usage': {'secret': 'SECRET_BODY'}}
        with self.mock_client(lambda _: httpx.Response(200, json=invalid)):
            with self.assertRaises(RespParseException) as error:
                await self.client.get_response(self.request)
        self.assertNotIn('SECRET_BODY', json.dumps(error.exception.ext_info))

    async def test_session_required(self):
        with self.assertRaises(ValueError):
            await self.client.get_response(self.request.copy_with(trace_context=None))

    async def test_background_session_is_stable_and_separate(self):
        headers = []
        def handler(request):
            headers.append(request.headers['x-opencode-session'])
            self.assertNotIn('person_evidence', headers[-1])
            return httpx.Response(200, json=self.native)
        with self.mock_client(handler):
            for kind in ('person_evidence', 'person_evidence', 'summary'):
                await self.client.get_response(self.request.copy_with(trace_context=RequestTraceContext(
                    task_name='utils', request_type=kind)))
        self.assertEqual(headers[0], headers[1])
        self.assertNotEqual(headers[0], headers[2])

    async def test_chat_cannot_use_background_session(self):
        with self.assertRaises(ValueError):
            await self.client.get_response(self.request.copy_with(trace_context=RequestTraceContext(
                task_name='replyer', request_type='maisaka.replyer')))

    async def test_existing_chat_session_has_priority(self):
        trace = RequestTraceContext(task_name='memory', request_type='summary', session_id='chat-session')
        self.assertEqual(module.request_session(trace), 'chat-session')

    async def test_credential_permissions(self):
        self.key.chmod(0o644)
        with self.assertRaises(ValueError):
            await self.client.get_response(self.request)

    async def test_unsupported_options(self):
        with self.assertRaises(ValueError):
            await self.client.get_response(self.request.copy_with(extra_params={'temperature': 1}))
        with self.assertRaises(ValueError):
            module.OpenCodeGoClient(APIProvider(name='bad', base_url='https://opencode.ai/zen/v1', auth_type='none'))

    async def test_interrupt_cancels_http(self):
        interrupted = asyncio.Event()
        interrupted.set()
        async def handler(_):
            await asyncio.sleep(60)
            return httpx.Response(200, json=self.native)
        with self.mock_client(handler):
            with self.assertRaises(ReqAbortException):
                await self.client.get_response(self.request.copy_with(interrupt_flag=interrupted))

    async def test_connect_timeout_trace_redacts_errors_and_does_not_retry(self):
        calls = []

        async def handler(request):
            calls.append(request)
            await request.extensions['trace']('connection.connect_tcp.started', {'host': 'opencode.ai'})
            raise httpx.ConnectTimeout('SECRET_BODY_AND_KEY', request=request)

        with self.mock_client(handler), patch.object(module.logger, 'warning') as warning:
            with self.assertRaises(NetworkConnectionError) as error:
                await self.client.get_response(self.request)
        self.assertEqual(len(calls), 1)
        self.assertIn('phase=tcp', warning.call_args.args[0])
        self.assertNotIn('SECRET_BODY_AND_KEY', str(error.exception))
        self.assertNotIn('SECRET_BODY_AND_KEY', warning.call_args.args[0])

    async def test_tls_stage_and_peer_are_logged_without_request_content(self):
        from types import SimpleNamespace

        async def handler(request):
            trace = request.extensions['trace']
            stream = SimpleNamespace(get_extra_info=lambda _: ('203.0.113.1', 443))
            await trace('connection.connect_tcp.complete', {'return_value': stream})
            await trace('connection.start_tls.started', {})
            raise httpx.ConnectTimeout('SECRET_BODY', request=request)

        with self.mock_client(handler), patch.object(module.logger, 'warning') as warning:
            with self.assertRaises(NetworkConnectionError):
                await self.client.get_response(self.request)
        self.assertIn('phase=tls peer=203.0.113.1', warning.call_args.args[0])
        self.assertNotIn('合成代码测试', warning.call_args.args[0])


if __name__ == '__main__':
    unittest.main()

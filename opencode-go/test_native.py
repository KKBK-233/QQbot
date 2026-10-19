"""用真实宿主SDK验证签名回放、参数白名单和可见正文隔离。"""
from copy import deepcopy
from dataclasses import replace
import os
import importlib.util
from pathlib import Path
import sys
import unittest

ROOT = Path('/home/qqbot/QQbot/MaiBot')
os.chdir(ROOT)
sys.path.insert(0, str(ROOT))
from src.config.model_configs import APIProvider
from src.llm_models.model_client.opencode_go_native import native_scope, native_item, convert_context
from src.llm_models.model_client.opencode_go_protocol import MODEL, messages_payload, response_parts, thinking_settings
from src.llm_models.payload_content.context_item import ContextItemBuilder, ContextItemMeta, ContextToolCall, RoleType, build_portable_output_items, get_response_text
from src.plugin_runtime.hook_payloads import serialize_prompt_items, deserialize_prompt_items
from src.llm_models.payload_content.context_item import CONTEXT_ITEM_SCHEMA_VERSION
from src.llm_models.payload_content.context_protocol import ContextProtocolMode


class NativeTests(unittest.TestCase):
    """同一轮签名及工具保持原样；篡改或跨供应商回放明确失败。"""

    def setUp(self):
        provider = APIProvider(name='Test', base_url='https://opencode.ai/zen/go/v1', client_type='opencode_go', auth_type='none')
        self.scope = native_scope(provider)
        self.content = [{'type': 'thinking', 'thinking': '', 'signature': 'FAKE_SIGNATURE'},
                        {'type': 'redacted_thinking', 'data': 'FAKE_REDACTED'},
                        {'type': 'text', 'text': '调用测试工具'},
                        {'type': 'tool_use', 'id': 'a', 'name': 'probe', 'input': {'n': 1}},
                        {'type': 'tool_use', 'id': 'b', 'name': 'probe', 'input': {'n': 2}}]
        self.portable = list(build_portable_output_items(content='调用测试工具', logical_turn_id='turn-1',
            tool_calls=[ContextToolCall.create(call_id=i, func_name='probe', args={'n': n}) for i, n in [('a', 1), ('b', 2)]]))
        self.items = [ContextItemBuilder().set_role(RoleType.User).add_text_content('合成测试').build(),
                      native_item(self.content, 'turn-1', self.scope), *self.portable]
        for call in ['a', 'b']:
            self.items.append(ContextItemBuilder().set_role(RoleType.Tool)
                              .set_meta(ContextItemMeta.create(logical_turn_id='turn-1'))
                              .set_tool_call_id(call).add_text_content('ok').build())

    def test_exact_native_replay_and_parallel_tools(self):
        payload = messages_payload(convert_context(self.items, self.scope))
        self.assertEqual(payload['messages'][1]['content'], self.content)
        self.assertEqual([b['tool_use_id'] for b in payload['messages'][2]['content']], ['a', 'b'])
        self.assertEqual(get_response_text(self.items[1:4]), '调用测试工具')

    def test_native_thinking_never_becomes_visible_reply(self):
        data = {'model': MODEL, 'stop_reason': 'tool_use', 'content': self.content}
        text, calls, _ = response_parts(data)
        self.assertEqual(text, '调用测试工具')
        self.assertEqual(len(calls), 2)
        self.assertNotIn('FAKE_SIGNATURE', text)

    def test_serialized_history_preserves_native_signature(self):
        wire = serialize_prompt_items(self.items)
        restored = deserialize_prompt_items(wire, item_schema_version=CONTEXT_ITEM_SCHEMA_VERSION,
                                            mode=ContextProtocolMode.REQUEST_CONTEXT, original_items=self.items)
        self.assertEqual(messages_payload(convert_context(restored, self.scope))['messages'][1]['content'], self.content)

    def test_wrong_scope_and_modified_projection_rejected(self):
        with self.assertRaises(ValueError):
            convert_context(self.items, replace(self.scope, provider_name='Other'))
        changed = list(self.items)
        changed[2] = ContextItemBuilder().set_role(RoleType.Assistant).set_meta(ContextItemMeta.create(logical_turn_id='turn-1')).add_text_content('篡改后').build()
        with self.assertRaises(ValueError):
            convert_context(changed, self.scope)

    def test_settings_whitelist_and_defaults(self):
        self.assertEqual(thinking_settings(), {'thinking': {'type': 'disabled'}})
        settings = {'thinking': {'type': 'adaptive', 'display': 'omitted'}, 'output_config': {'effort': 'low'}}
        self.assertEqual(thinking_settings(settings), settings)
        for invalid in [{'api_key': 'FAKE'}, {'thinking': {'type': 'enabled'}}, {'output_config': {'effort': 'max'}}, {'output_config': {'format': {}}}]:
            with self.assertRaises(ValueError):
                thinking_settings(invalid)

    def test_missing_signature_or_tool_result_rejected(self):
        bad = deepcopy(self.content)
        del bad[0]['signature']
        with self.assertRaises(ValueError):
            native_item(bad, 'turn-1', self.scope)
        with self.assertRaises(ValueError):
            messages_payload(convert_context(self.items[:-1], self.scope))

    def test_old_orphan_text_history_does_not_restore_removed_analysis(self):
        content = [{'type': 'text', 'text': '旧规划分析'}]
        native = native_item(content, 'old-turn', self.scope)
        self.assertEqual(convert_context([native], self.scope), [])
        visible = build_portable_output_items(content='宿主更新后的正文', logical_turn_id='old-turn')
        projected = convert_context([native, *visible], self.scope)
        self.assertEqual(projected, [{'role': 'assistant', 'content': '宿主更新后的正文'}])


class TaskHookReplayTests(unittest.IsolatedAsyncioTestCase):
    """通过真实任务插件入口回放，防止语气过滤重新破坏工具和思考历史。"""

    async def test_planner_hook_keeps_complete_native_history(self):
        from types import SimpleNamespace
        folder = Path('/mnt/d/code/web/QQbot/MaiBot/plugins/qqbot-task-assist')
        spec = importlib.util.spec_from_file_location('task_hook_replay_probe', folder / 'plugin.py',
                                                     submodule_search_locations=[str(folder)])
        plugin = importlib.util.module_from_spec(spec)
        sys.modules[spec.name] = plugin
        spec.loader.exec_module(plugin)
        fixture = NativeTests()
        fixture.setUp()
        wire = serialize_prompt_items(fixture.items)
        host = SimpleNamespace(flow=plugin.TaskFlow(), ctx=SimpleNamespace())
        output = await plugin.TaskAssistPlugin.before(host, session_id='isolated-test', items=wire,
                                                      tool_definitions=[])
        modified = output['modified_kwargs']['items']
        self.assertEqual(modified, wire)
        restored = deserialize_prompt_items(modified, item_schema_version=CONTEXT_ITEM_SCHEMA_VERSION,
                                            mode=ContextProtocolMode.REQUEST_CONTEXT, original_items=fixture.items)
        payload = messages_payload(convert_context(restored, fixture.scope))
        self.assertEqual(payload['messages'][1]['content'], fixture.content)
        self.assertEqual([b['tool_use_id'] for b in payload['messages'][2]['content']], ['a', 'b'])
        self.assertFalse(hasattr(plugin.TaskAssistPlugin, 'tone_before'))


if __name__ == '__main__':
    unittest.main()

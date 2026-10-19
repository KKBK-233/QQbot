"""验证固定前缀缓存布局，不读取真实凭据或请求模型。"""
from copy import deepcopy
import json
from pathlib import Path
import tempfile
import unittest

from cache_layout import with_prompt_cache, numeric_usage, record_cache_usage, RULE_PREFIX, STATUS_MARKER, PRIORITY_PREFIX


class LayoutTests(unittest.TestCase):
    """校验动态状态仍为系统规则、工具与聊天内容无删减。"""

    def payload(self, clock='2026-10-07 12:00:00', state='running', addressed=False):
        system = [{'type': 'text', 'text': '固定宿主规则'},
                  {'type': 'text', 'text': '【宿主本轮上下文】 时间：' + clock},
                  {'type': 'text', 'text': '固定语气规则'}]
        if addressed:
            system.append({'type': 'text', 'text': PRIORITY_PREFIX + '这是直接问话'})
        system.append({'type': 'text', 'text': RULE_PREFIX + '固定角色和工具规则' + STATUS_MARKER + state})
        return {'model': 'claude-haiku-5-5', 'system': system, 'thinking': {'type': 'disabled'},
                'tools': [{'name': 'z', 'input_schema': {'required': ['b','a'], 'properties': {'b': {}, 'a': {}}}},
                          {'name': 'a', 'description': '说明'}],
                'messages': [{'role': 'user', 'content': [{'type': 'text', 'text': '真实历史和当前问题'}]}]}

    def test_dynamic_clock_and_status_do_not_change_prefix(self):
        first = with_prompt_cache(self.payload())
        second = with_prompt_cache(self.payload(clock='2026-10-07 12:00:01', state='succeeded', addressed=True))
        def cached_prefix(payload):
            prefix=[]
            for block in payload['system']:
                prefix.append(block)
                if 'cache_control' in block:break
            return prefix
        self.assertEqual(cached_prefix(first),cached_prefix(second))
        self.assertNotEqual(first['system'],second['system'])

    def test_content_roles_and_original_are_preserved(self):
        original=self.payload(addressed=True)
        untouched=deepcopy(original)
        result=with_prompt_cache(original)
        self.assertEqual(original,untouched)
        self.assertEqual(result['messages'],original['messages'])
        self.assertEqual(result['thinking'],original['thinking'])
        texts=[b['text'] for b in result['system']]
        self.assertIn(STATUS_MARKER+'running',texts)
        self.assertIn(PRIORITY_PREFIX+'这是直接问话',texts)
        self.assertEqual(sum(map(len,texts)),sum(len(b['text']) for b in original['system']))

    def test_tools_are_stable_without_schema_semantic_change(self):
        first=self.payload()
        second=deepcopy(first);second['tools'].reverse()
        self.assertEqual(with_prompt_cache(first)['tools'],with_prompt_cache(second)['tools'])
        self.assertEqual(with_prompt_cache(first)['tools'][1]['input_schema']['required'], ['b','a'])
        self.assertNotIn('cache_control',with_prompt_cache(first)['tools'][0])

    def test_no_history_cache_or_dynamic_only_marker(self):
        payload={'system':[{'type':'text','text':'【宿主本轮上下文】 当前时间：2026-10-07 12:00:00'}],
                 'messages':[{'role':'user','content':[{'type':'text','text':'历史'}]}]}
        result=with_prompt_cache(payload)
        self.assertNotIn('cache_control',result['system'][0])
        self.assertEqual(result['messages'],payload['messages'])

    def test_unknown_instruction_is_not_split(self):
        payload={'system':[{'type':'text','text':'用户引用了'+STATUS_MARKER+'不是已知宿主状态'}]}
        self.assertEqual(with_prompt_cache(payload)['system'][0]['text'],payload['system'][0]['text'])

    def test_markers_at_most_two(self):
        result=with_prompt_cache(self.payload())
        self.assertEqual(json.dumps(result).count('cache_control'),2)

    def test_usage_and_missing_counters(self):
        self.assertIsNone(numeric_usage({})['cache_read_input_tokens'])
        self.assertEqual(numeric_usage({'usage':{'input_tokens':30,'cache_read_input_tokens':9600}})['input_tokens'],30)
        with tempfile.TemporaryDirectory() as folder:
            path=Path(folder)/'private/usage.jsonl'
            self.assertTrue(record_cache_usage(path, {'usage':{'input_tokens':30,'cache_read_input_tokens':9600,'cache_creation_input_tokens':0,'output_tokens':14}},'planner','a'*32))
            row=json.loads(path.read_text())
            self.assertEqual(row['total_input_tokens'],9630)
            self.assertEqual(row['token_hit_rate_percent'],99.69)
            self.assertEqual(path.stat().st_mode & 0o777,0o600)
            self.assertTrue(record_cache_usage(path,{},'SECRET_TEXT','SECRET_TEXT'))
            rows=path.read_text().splitlines()
            self.assertNotIn('SECRET_TEXT',rows[-1])
            self.assertIsNone(json.loads(rows[-1])['total_input_tokens'])

    def test_telemetry_failure_does_not_raise(self):
        with tempfile.TemporaryDirectory() as folder:
            path=Path(folder)/'file';path.write_text('blocked')
            self.assertFalse(record_cache_usage(path/'usage.jsonl',{},'planner','a'*32))


if __name__=='__main__':
    unittest.main()

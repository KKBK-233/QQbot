"""离线验证原生Messages转换、严格型号校验和缓存计数。"""
import copy
import unittest

from protocol import BASE, MODEL, content_blocks, messages_payload, response_parts, response_metadata


class ProtocolTests(unittest.TestCase):
    """只使用合成消息，不读取凭据、不请求网络。"""

    def test_fixed_model_and_no_fallback(self):
        p = messages_payload([{'role': 'user', 'content': '代码测试'}])
        self.assertEqual(p['model'], MODEL)
        self.assertFalse(p['stream'])
        self.assertEqual(p['thinking'], {'type': 'disabled'})
        self.assertEqual(BASE, 'https://opencode.ai/zen/go/v1')

    def test_system_and_tools(self):
        p = messages_payload([{'role': 'system', 'content': '规则'}, {'role': 'user', 'content': '代码测试'}],
                             [{'name': 'probe', 'parameters': {'type': 'object', 'properties': {}}}])
        self.assertEqual(p['system'][0]['text'], '规则')
        self.assertEqual(p['tools'][0]['input_schema']['type'], 'object')

    def test_parallel_tool_results(self):
        messages = [{'role': 'user', 'content': '测试'}, {'role': 'assistant', 'content': None,
            'tool_calls': [{'id': i, 'type': 'function', 'function': {'name': 'probe', 'arguments': '{}'}} for i in ['a', 'b']]},
            {'role': 'tool', 'tool_call_id': 'a', 'content': 'ok'},
            {'role': 'tool', 'tool_call_id': 'b', 'content': 'ok'}]
        p = messages_payload(messages)
        self.assertEqual([b['tool_use_id'] for b in p['messages'][-1]['content']], ['a', 'b'])
        with self.assertRaises(ValueError):
            messages_payload(messages[:-1])

    def test_orphan_result(self):
        with self.assertRaises(ValueError):
            messages_payload([{'role': 'tool', 'tool_call_id': 'x', 'content': 'ok'}])

    def test_invalid_arguments_and_roles(self):
        for args in ['[]', 'not-json']:
            with self.assertRaises(ValueError):
                messages_payload([{'role': 'user', 'content': '测试'}, {'role': 'assistant', 'tool_calls':
                    [{'id': 'x', 'type': 'function', 'function': {'name': 'probe', 'arguments': args}}]}])
        with self.assertRaises(ValueError):
            messages_payload([{'role': 'assistant', 'content': '不能由助手开场'}])

    def test_image_and_external_rejection(self):
        blocks = content_blocks([{'type': 'image_url', 'image_url': {'url': 'data:image/png;base64,AA=='}}])
        self.assertEqual(blocks[0]['source']['media_type'], 'image/png')
        with self.assertRaises(ValueError):
            content_blocks([{'type': 'image_url', 'image_url': {'url': 'https://example.com/a.png'}}])

    def test_history_window_starting_with_assistant(self):
        messages = [{'role': 'system', 'content': '角色规则'},
                    {'role': 'assistant', 'content': '此前的回答'},
                    {'role': 'user', 'content': '最新的问题'}]
        original = copy.deepcopy(messages)
        payload = messages_payload(messages)
        self.assertEqual([m['role'] for m in payload['messages']], ['user', 'assistant', 'user'])
        self.assertEqual(payload['messages'][1]['content'], [{'type': 'text', 'text': '此前的回答'}])
        self.assertEqual(payload['messages'][-1]['content'], [{'type': 'text', 'text': '最新的问题'}])
        self.assertEqual(messages, original)

    def test_leading_native_tool_turn_preserves_signature_and_results(self):
        native = [{'type': 'thinking', 'thinking': '', 'signature': 'FAKE_TEST_SIGNATURE'},
                  {'type': 'text', 'text': '此前的分析'},
                  {'type': 'tool_use', 'id': 'a', 'name': 'probe', 'input': {}}]
        messages = [{'role': 'assistant', 'native_content': native},
                    {'role': 'tool', 'tool_call_id': 'a', 'content': '结果'}]
        payload = messages_payload(messages)
        self.assertEqual(payload['messages'][1]['content'], native)
        self.assertEqual(payload['messages'][2]['content'][0]['tool_use_id'], 'a')
        with self.assertRaises(ValueError):
            messages_payload(messages[:1] + [{'role': 'user', 'content': '缺少工具结果'}])

    def test_complete_response_and_usage(self):
        d = {'model': MODEL, 'stop_reason': 'end_turn', 'content': [{'type': 'text', 'text': '通过'}],
             'usage': {'input_tokens': 10, 'output_tokens': 5, 'cache_read_input_tokens': 30,
                       'cache_creation_input_tokens': 20}}
        text, calls, usage = response_parts(d)
        self.assertEqual((text, calls, usage['prompt'], usage['cache_hit'], usage['cache_miss']), ('通过', [], 60, 30, 30))
        for model, stop in [('other', 'end_turn'), (MODEL, 'max_tokens'), (MODEL, 'tool_use')]:
            invalid = copy.deepcopy(d)
            invalid.update(model=model, stop_reason=stop)
            with self.assertRaises(ValueError):
                response_parts(invalid)

    def test_tool_response(self):
        d = {'model': MODEL, 'stop_reason': 'tool_use', 'content':
             [{'type': 'tool_use', 'id': 'x', 'name': 'probe', 'input': {}}]}
        self.assertEqual(response_parts(d)[1][0]['id'], 'x')
        d['content'].append(copy.deepcopy(d['content'][0]))
        with self.assertRaises(ValueError):
            response_parts(d)

    def test_malformed_response_and_metadata(self):
        for data in [[], {}, {'model': MODEL, 'content': [None]},
                     {'model': MODEL, 'stop_reason': 'end_turn', 'content': [{'type': 'text', 'text': 'ok'}], 'usage': {'input_tokens': -1}}]:
            with self.assertRaises(ValueError):
                response_parts(data)
            self.assertIsInstance(response_metadata(data), dict)
        self.assertEqual(response_metadata({'stop_reason': [], 'content': [{'type': {}}]})['stop_reason'], 'unknown')

    def test_token_budget(self):
        for budget in [0, 8193, '100']:
            with self.assertRaises(ValueError):
                messages_payload([{'role': 'user', 'content': '测试'}], max_tokens=budget)


if __name__ == '__main__':
    unittest.main()

"""检查连续工具消息的推理对应关系，避免内容丢失或污染用户数据。"""
import unittest
from types import SimpleNamespace
from deepseek_thinking_compat import convert_thinking_messages


def item(kind, **values):
    return type(kind, (SimpleNamespace,), {})(**values)


class ThinkingProtocolTests(unittest.TestCase):
    def test_reasoning_stays_with_its_tool_turn(self):
        user = item('UserMessageItem', content='用户材料')
        output = item('FunctionCallOutputItem', content='工具资料')
        items = [user, item('ReasoningItem', text_parts=('原生推理',)),
                 item('FunctionCallItem', call='first'), output,
                 item('FunctionCallItem', call='old-nonthinking')]
        def converter(group):
            return ([{'role': 'assistant', 'content': None, 'tool_calls': [value.call for value in group if hasattr(value, 'call')]}]
                    if any(hasattr(value, 'call') for value in group) else
                    [{'role': 'user' if value is user else 'tool', 'content': value.content} for value in group if hasattr(value, 'content')])
        result = convert_thinking_messages(items, converter)
        self.assertEqual(result[0], {'role': 'user', 'content': '用户材料'})
        self.assertEqual(result[1]['reasoning_content'], '原生推理')
        self.assertEqual(result[1]['tool_calls'], ['first'])
        self.assertEqual(result[2], {'role': 'tool', 'content': '工具资料'})
        self.assertEqual(result[3]['reasoning_content'], '')
        self.assertFalse(hasattr(items[1], 'reasoning_content'))

    def test_summary_is_not_native_reasoning(self):
        values = [item('ReasoningItem', summary_parts=('摘要',)), item('AssistantMessageItem', content='正文')]
        projected = convert_thinking_messages(values, lambda _: [{'role': 'assistant', 'content': '正文'}])
        self.assertEqual(projected, [{'role': 'assistant', 'content': '正文', 'reasoning_content': ''}])


if __name__ == '__main__':
    unittest.main()

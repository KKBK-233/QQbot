"""从真实宿主源码抽取桥接方法，验证模型不能伪造沙箱配额身份。"""

import ast
from pathlib import Path
from types import SimpleNamespace as NS
import unittest


class ContextTests(unittest.TestCase):
    """不启动宿主或模型，只执行部署后的真实桥接函数。"""

    @classmethod
    def setUpClass(cls):
        path = Path('/home/qqbot/QQbot/MaiBot/src/plugin_runtime/component_query.py')
        tree = ast.parse(path.read_text())
        original = next(n for n in tree.body if isinstance(n, ast.ClassDef) and n.name == 'ComponentQueryService')
        original.body = [n for n in original.body if isinstance(n, ast.FunctionDef) and
                         n.name in ('_build_tool_context_payload', '_build_tool_invocation_payload')]
        module = ast.Module(body=[ast.ImportFrom(module='__future__', names=[ast.alias(name='annotations')], level=0), original], type_ignores=[])
        ast.fix_missing_locations(module)
        namespace = {}
        exec(compile(module, str(path), 'exec'), namespace)
        cls.bridge = namespace['ComponentQueryService']

    def test_spoofed_scope_overridden(self):
        context = NS(stream_id='real-session', session_id='real-session', group_id='real-group',
                     user_id='real-user', platform='qq', metadata={})
        invocation = NS(arguments={'code': 'print(1)', 'stream_id': 'fake', 'chat_id': 'fake', 'user_id': 'fake',
                                   'group_id': 'fake', '_sandbox_trusted_context': False})
        entry = NS(plugin_id='local.qqbot-python-sandbox', invoke_method='plugin.invoke_tool')
        result = self.bridge._build_tool_invocation_payload(entry, invocation, context)
        self.assertEqual(result['stream_id'], 'real-session')
        self.assertEqual(result['user_id'], 'real-user')
        self.assertEqual(result['group_id'], 'real-group')
        self.assertIs(result['_sandbox_trusted_context'], True)

    def test_missing_context_cannot_be_forged(self):
        entry = NS(plugin_id='local.qqbot-python-sandbox', invoke_method='plugin.invoke_tool')
        invocation = NS(arguments={'stream_id': 'fake', 'user_id': 'fake', '_sandbox_trusted_context': True})
        result = self.bridge._build_tool_invocation_payload(entry, invocation, None)
        self.assertIs(result['_sandbox_trusted_context'], False)
        self.assertNotIn('user_id', result)

    def test_other_plugins_unchanged(self):
        entry = NS(plugin_id='local.other', invoke_method='plugin.invoke_tool')
        invocation = NS(arguments={'code': 'x', 'stream_id': 'existing'})
        self.assertEqual(self.bridge._build_tool_invocation_payload(entry, invocation, None), invocation.arguments)


if __name__ == '__main__':
    unittest.main()

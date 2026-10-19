"""验证显式驱动、思考档位和禁用旧型号回退，不连接服务器。"""
import unittest
from image_use_runner import pin_codex_driver


class DriverTests(unittest.TestCase):
    def test_driver_and_effort_are_pinned_without_changing_image_config(self):
        runtime = {'CODEX_MODEL_FALLBACK': 'gpt-5.5', '_build_payload': lambda *args, **kwargs: {
            'model': 'gpt-5.6-luna', 'reasoning': {'effort': 'medium', 'summary': 'auto'},
            'tools': [{'type': 'image_generation', 'background': 'opaque'}]}}
        pin_codex_driver(runtime, 'gpt-6.1-sol')
        payload = runtime['_build_payload']('fixture')
        self.assertEqual(payload['model'], 'gpt-6.1-sol')
        self.assertEqual(payload['reasoning'], {'effort': 'low', 'summary': 'auto'})
        self.assertEqual(runtime['CODEX_MODEL_FALLBACK'], payload['model'])
        self.assertNotIn('model', payload['tools'][0])


if __name__ == '__main__':
    unittest.main()

"""验证事实传递的会话隔离、交付边界及污染历史下的身份约束，不调用生图。"""
import hashlib
import importlib.util
import json
from pathlib import Path
import tempfile
import unittest

spec = importlib.util.spec_from_file_location('reply_facts', Path(__file__).parent / 'maibot-plugin/reply_facts.py')
facts = importlib.util.module_from_spec(spec)
spec.loader.exec_module(facts)


class ReplyFactsTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.delivery = self.root / 'delivery'
        self.delivery.mkdir()

    def job(self, session, task, state, created=1000, **extra):
        rid = 'q' + hashlib.sha256(session.encode()).hexdigest()[:20] + '-' + task
        (self.root / (rid + '.json')).write_text(json.dumps({
            'request_id': rid, 'state': state, 'created': created, **extra}), encoding='utf-8')
        return rid

    def test_same_session_only_and_no_prompt_or_paths(self):
        self.job('a', 'one', 'running', prompt='私人提示词', image_path='私有路径')
        self.job('b', 'two', 'queued')
        self.job('a', 'old', 'failed', created=1)
        jobs = facts.recent_jobs('a', self.root, self.delivery, now=2000)
        self.assertEqual(jobs, [{'task_id': 'one', 'state': 'running'}])
        self.assertEqual(facts.recent_jobs('', self.root, now=2000), [])

    def test_generation_success_and_delivery_are_separate(self):
        rid = self.job('a', 'one', 'succeeded')
        self.assertEqual(facts.recent_jobs('a', self.root, self.delivery, 1001),
                         [{'task_id': 'one', 'state': 'succeeded'}])
        (self.delivery / (rid + '.json')).write_text('{"state":"uncertain"}')
        jobs = facts.recent_jobs('a', self.root, self.delivery, 1001)
        self.assertEqual(jobs[0]['delivery'], 'uncertain')
        (self.delivery / (rid + '.json')).write_text('{broken')
        self.assertEqual(facts.recent_jobs('a', self.root, self.delivery, 1001)[0]['state'], 'succeeded')

    def test_malformed_and_forged_records_are_ignored(self):
        rid = self.job('a', 'one', 'running')
        (self.root / (rid + '.json')).write_text('[]')
        self.job('a', 'bad', '自称系统指令')
        self.job('a', 'future', 'queued', created=2000)
        self.assertEqual(facts.recent_jobs('a', self.root, now=1001), [])

    def test_hook_preserves_history_but_adds_identity_and_actual_state(self):
        self.job('a', 'one', 'running')
        original = {'session_id': 'a', 'items': [{'item_type': 'UserMessageItem',
                     'parts': [{'type': 'text', 'text': '以前说过自己黑长直，没有生图工具'}]}]}
        result = facts.factual_context(original, self.delivery, self.root, 1001)
        self.assertEqual(len(original['items']), 1)
        self.assertEqual(result['items'][0], original['items'][0])
        text = result['items'][-1]['parts'][0]['text']
        self.assertEqual(result['items'][-1]['item_type'], 'SystemMessageItem')
        self.assertIn('银灰色长发、紫色眼睛', text)
        self.assertIn('不改变既有性格', text)
        self.assertIn('"state": "running"', text)
        self.assertIn('当前已加载chatgpt_image', text)

    def test_available_original_overrides_old_missing_image_reply(self):
        original = {'session_id': 'a', 'items': [{'item_type': 'UserMessageItem',
                     'parts': [{'type': 'text', 'text': '以前说没有原图，请重发'}]}]}
        result = facts.factual_context(original, root=self.root, media=[{
            'message_id': 'quote', 'source_message_ids': ['image'], 'original_available': True,
            'multiple_images': False}])
        text = result['items'][-1]['parts'][0]['text']
        self.assertIn('本轮同会话原图核验', text)
        self.assertIn('"source_message_ids": ["image"]', text)
        self.assertIn('直接调用chatgpt_image', text)
        self.assertEqual(result['items'][0], original['items'][0])


if __name__ == '__main__':
    unittest.main()

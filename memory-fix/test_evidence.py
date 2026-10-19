"""验证原句完整性、作者证据和画像分层，防止过度放宽校验。"""
import unittest
from evidence_policy import canonical_record


class EvidenceTests(unittest.TestCase):
    def test_independent_sentence(self):
        item = canonical_record('我喜欢猫。我使用Linux。', '我喜欢猫。', '小明', 'self')
        self.assertEqual(item['text'], '小明喜欢猫')
        self.assertEqual(item['section'], 'stable_facts')

    def test_preference(self):
        item = canonical_record('我希望回答简短。', '我希望回答简短。', '小明', 'self')
        self.assertEqual(item['section'], 'interaction_preferences')

    def test_qq_mentions_and_memory_request(self):
        for text in ('记住，我喜欢猫', '@娜娜 记住：我喜欢猫', '我喜欢猫 @KKBK'):
            self.assertEqual(canonical_record(text, '我喜欢猫', '小明', 'self')['text'], '小明喜欢猫')

    def test_interaction_is_not_personality(self):
        item = canonical_record('帮我查一下阿隆索的比赛结果', '帮我查一下阿隆索的比赛结果', '小明', 'self')
        self.assertEqual(item['section'], 'recent_interactions')
        self.assertIn('在交流中说', item['text'])

    def test_pollution(self):
        for text, quote in [('我喜欢猫，但现在不喜欢了', '我喜欢猫'),
                            ('朋友说：我喜欢猫。', '我喜欢猫。'),
                            ('我喜欢猫。才怪', '我喜欢猫。'),
                            ('例如我喜欢猫。', '我喜欢猫。'),
                            ('我喜欢猫。不要记', '我喜欢猫。'),
                            ('只是测试，我喜欢猫。', '我喜欢猫。'),
                            ('我喜欢猫。', '我喜欢狗。')]:
            with self.subTest(text=text):
                self.assertIsNone(canonical_record(text, quote, '小明', 'self'))

    def test_question_and_temporary_are_not_stable(self):
        for text in ['我喜欢猫吗？', '我今天想要一杯咖啡']:
            item = canonical_record(text, text, '小明', 'self')
            self.assertTrue(item is None or item['stability'] == 'temporal')


if __name__ == '__main__':
    unittest.main()

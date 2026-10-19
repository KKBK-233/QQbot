"""验证聊天纠正可持久化并自动用于绘图，同时保留来源、隔离与撤销。"""
from pathlib import Path
from concurrent.futures import ThreadPoolExecutor
import sys
import tempfile
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parent / 'maibot-plugin'))
from character_memory import CharacterMemory, learning_evidence
from character_rules import anchored_prompt


class MemoryTests(unittest.TestCase):
    def test_parallel_updates_preserve_revision_and_history(self):
        with tempfile.TemporaryDirectory() as root:
            store = CharacterMemory(root)
            def update(number):
                return store.remember('g', 'chibi', 'hair', '头发修正' + str(number),
                                      str(number), '123', character='claude')
            with ThreadPoolExecutor(max_workers=4) as pool:
                list(pool.map(update, range(12)))
            state = store.read('g', 'chibi', 'claude')
            self.assertEqual(state['revision'], 12)
            self.assertEqual(len(state['history']), 12)
            self.assertEqual(store.undo('g', 'chibi', 'claude')['revision'], 13)

    def test_roles_and_legacy_ds_memory_are_separate(self):
        with tempfile.TemporaryDirectory() as root:
            store = CharacterMemory(root)
            ds = store.remember('g', 'chibi', 'tail', '鲸尾保持短尾柄', 'ds', '123')
            # 模拟旧版本没有角色字段的记忆，继续按原路径加载。
            ds.pop('character')
            store.save(ds)
            store.remember('g', 'chibi', 'hair', '头发改成粉色', 'claude', '123', character='claude')
            self.assertEqual(store.read('g', 'chibi')['facts']['tail']['text'], '鲸尾保持短尾柄')
            self.assertEqual(store.read('g', 'chibi', 'gpt')['revision'], 0)
            self.assertEqual(store.read('g', 'standard', 'claude')['revision'], 0)
            self.assertEqual(store.read('other', 'chibi', 'claude')['revision'], 0)
            store.undo('g', 'chibi', 'claude')
            self.assertEqual(store.read('g', 'chibi', 'claude')['facts'], {})
            self.assertEqual(store.read('g', 'chibi')['revision'], 1)

    def test_restart_isolation_replacement_and_undo(self):
        with tempfile.TemporaryDirectory() as root:
            store = CharacterMemory(root)
            state = store.remember('group-a', 'chibi', 'tail', '尾鳍只有左右两片', 'm1', '123')
            same = store.remember('group-a', 'chibi', 'tail', '尾鳍只有左右两片', 'm1', '123')
            self.assertEqual(state['revision'], same['revision'])
            restarted = CharacterMemory(root)
            self.assertEqual(restarted.features(restarted.read('group-a', 'chibi')), ['尾鳍只有左右两片'])
            self.assertEqual(restarted.read('group-b', 'chibi')['revision'], 0)
            self.assertEqual(restarted.read('group-a', 'standard')['revision'], 0)
            restarted.remember('group-a', 'chibi', 'tail', '尾柄连后腰、尾鳍中央有V形缺口', 'm2', '123')
            current = restarted.read('group-a', 'chibi')
            self.assertEqual(len(current['facts']), 1)
            self.assertEqual(current['facts']['tail']['source_message_id'], 'm2')
            restored = restarted.undo('group-a', 'chibi')
            self.assertEqual(restored['facts']['tail']['source_message_id'], 'm1')

    def test_elaina_memory_does_not_reuse_gpt_profile(self):
        with tempfile.TemporaryDirectory() as root:
            store = CharacterMemory(root)
            store.remember('g', 'standard', 'outfit', '衣服换成旅行服', 'elaina-m1', '123', character='elaina')
            self.assertEqual(store.read('g', 'standard', 'elaina')['revision'], 1)
            self.assertEqual(store.read('g', 'standard', 'gpt')['facts'], {})
            self.assertEqual(store.read('g', 'chibi', 'elaina')['facts'], {})

    def test_source_cannot_be_cross_group_bot_or_invented(self):
        msg = {'session_id': 'g', 'message_info': {'user_info': {'user_id': '123'}},
               'raw_message': [{'type': 'text', 'data': '记住：尾鳍只有左右两片'}]}
        self.assertEqual(learning_evidence(msg, 'g', '尾鳍只有左右两片'), '123')
        with self.assertRaises(ValueError):
            learning_evidence(msg, 'other', '尾鳍只有左右两片')
        with self.assertRaises(ValueError):
            learning_evidence(msg, 'g', '紫色短发')
        msg['message_info']['user_info']['user_id'] = '3976405558'
        with self.assertRaises(ValueError):
            learning_evidence(msg, 'g', '尾鳍只有左右两片')

    def test_learned_facts_and_new_reference_override_old_defaults(self):
        prompt = anchored_prompt('DS娘Q版微笑', 1, form='chibi',
                                  learned=['以后DS娘用紫色长发'], learned_reference=True)
        self.assertIn('【本群已确认的动态修正】以后DS娘用紫色长发', prompt)
        self.assertIn('聊天中新确认的角色标准', prompt)
        self.assertNotIn('严格保留注册的渐变蓝色', prompt)


if __name__ == '__main__':
    unittest.main()

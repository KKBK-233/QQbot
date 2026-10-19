"""角色参考只影响本群的明确DS娘请求，不调用模型或生图。"""

from pathlib import Path
import sys
import shutil
import tempfile
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parent / 'maibot-plugin'))
from character_rules import character_reference, character_references, character_requested, character_form, anchored_prompt, verified_appearance_change, DS_REFERENCES
from character_rules import requested_characters, registered_reference


class CharacterTests(unittest.TestCase):
    def test_explicit_new_roles_and_ordinary_dragon(self):
        self.assertEqual(requested_characters('Claude娘和GPT龙娘Q版'), ['claude', 'gpt'])
        self.assertEqual(requested_characters('ChatGPT娘和克劳德娘'), ['gpt', 'claude'])
        self.assertEqual(requested_characters('换表情', 'gpt'), ['gpt'])
        self.assertEqual(requested_characters('用GPT画一条龙'), [])
        self.assertEqual(character_form('Claude娘普通版表情包'), 'standard')
        self.assertIsNone(character_reference(next(iter(DS_REFERENCES)), 'Claude娘Q版'))

    def test_public_reference_is_checked_and_cannot_be_replaced_silently(self):
        assets = Path(__file__).parent / 'character-assets'
        with tempfile.TemporaryDirectory() as root:
            shutil.copy2(assets / 'claude-chibi.webp', root)
            self.assertEqual(registered_reference('claude', 'chibi', root), 'claude-chibi.webp')
            Path(root, 'claude-chibi.webp').write_bytes(b'changed')
            with self.assertRaises(ValueError):
                registered_reference('claude', 'chibi', root)

    def test_elaina_names_and_conflicting_model_parameter(self):
        # 回归群内真实错误：文字点名伊蕾娜却填写GPT角色，必须在附图前拒绝。
        for name in ('伊蕾娜', '伊雷娜', 'Elaina', 'イレイナ'):
            self.assertEqual(requested_characters(name + '穿白丝在卧室站立'), ['elaina'])
            with self.assertRaisesRegex(ValueError, '角色名冲突'):
                requested_characters(name + '穿白丝在卧室站立', 'gpt')
        self.assertEqual(requested_characters('换表情', 'elaina'), ['elaina'])
        self.assertEqual(requested_characters('伊蕾娜和GPT娘一起喝茶', 'elaina'), ['elaina', 'gpt'])
        prompt = anchored_prompt('伊蕾娜在窗边喝茶', 1, character='elaina')
        self.assertIn('第1张附图只用于确定伊蕾娜', prompt)
        self.assertIn('官方角色设定图', prompt)
        self.assertNotIn('GPT娘（龙娘）的注册外观', prompt)

    def test_new_cards_do_not_inherit_ds_identity_or_white_reference_background(self):
        for role in ('claude', 'gpt'):
            prompt = anchored_prompt('在花园里微笑', 1, form='chibi', character=role)
            self.assertNotIn('鲸尾', prompt)
            self.assertNotIn('女仆', prompt)
            self.assertIn('在花园里微笑', prompt)
            self.assertIn('自行设计合适的具体场景', prompt)
            self.assertIn('不透明背景', prompt)

    def test_only_explicit_ds_girl_in_approved_group_uses_reference(self):
        session = next(iter(DS_REFERENCES))
        for prompt in ('DS娘Q版表情', 'DeepSeek娘新姿势', 'DS酱', '鲸鱼娘'):
            self.assertEqual(character_reference(session, prompt), '-1178010614' if 'Q版' in prompt else '-1461680975')
        self.assertIsNone(character_reference(session, '蓝色大肥鱼'))
        self.assertIsNone(character_reference('another-group', 'DS娘'))
        self.assertEqual(character_reference(session, '换表情', 'deepseek'), '-1461680975')
        with self.assertRaises(ValueError):
            character_reference(session, '画图', '../other')

    def test_identity_and_request_are_separate_and_reference_order_is_explicit(self):
        prompt = anchored_prompt('Q版微笑表情', 2)
        self.assertIn('第2张附图', prompt)
        self.assertIn('渐变蓝色披散长卷发', prompt)
        self.assertIn('2.5头身', prompt)
        self.assertNotIn('麻花辫', prompt)
        self.assertIn('【本次画面要求】Q版微笑表情', prompt)

    def test_role_reference_does_not_make_new_drawing_transparent(self):
        prompt = anchored_prompt('Q版聊天表情包，简单背景', 1)
        self.assertIn('不透明背景', prompt)
        self.assertIn('短尾柄', prompt)
        self.assertIn('尾柄连续过渡', prompt)
        self.assertNotIn('【新画背景】', anchored_prompt('Q版透明背景贴纸', 1))
        self.assertNotIn('【新画背景】', anchored_prompt('只改表情', 2, has_original=True))
        self.assertIn('【编辑背景】', anchored_prompt('只改表情', 2, has_original=True))
        self.assertIn('自行设计合适的具体场景', anchored_prompt('DS娘在数钱', 1))
        self.assertNotIn('【编辑背景】', anchored_prompt('保留原图透明度，只改表情', 2, has_original=True))
        self.assertIn('不透明背景', anchored_prompt('DS娘不要透明背景', 1))

    def test_old_hallucinated_appearance_is_removed_without_losing_scene(self):
        prompt = anchored_prompt('DS娘,Q版,二头身,紫色短发,褐色眼睛,猫耳,微笑,紫色背景,purple short hair', 1)
        self.assertNotIn('紫色短发', prompt)
        self.assertNotIn('褐色眼睛', prompt)
        self.assertNotIn('猫耳', prompt)
        self.assertNotIn('purple short hair', prompt)
        self.assertIn('微笑', prompt)
        self.assertIn('紫色背景', prompt)
        self.assertIn('鲸尾', prompt)
        self.assertIn('不能画成鱼尾替代双腿', prompt)
        self.assertNotIn('二头身', prompt)

    def test_forms_select_independent_references_and_keep_girl_loose_hair(self):
        session = next(iter(DS_REFERENCES))
        self.assertEqual(character_reference(session, 'DS娘微笑', form='chibi'), '-1178010614')
        self.assertEqual(character_reference(session, 'DS娘Q版', form='standard'), '-1461680975')
        prompt = anchored_prompt('DS娘少女版微笑', 1, form='standard', reference_count=2)
        self.assertIn('披散长卷发', prompt)
        self.assertIn('第1、2张附图', prompt)
        self.assertNotIn('长麻花辫', prompt)
        self.assertEqual(character_references(session, 'DS娘少女版'), ['-1461680975', '-293459031'])
        self.assertEqual(character_references('another-group', 'DS娘少女版'), [])
        self.assertEqual(character_form('DS娘少女版表情包'), 'standard')
        self.assertEqual(character_form('DS娘非Q版'), 'standard')
        self.assertEqual(character_form('DS娘少女版Q版'), 'chibi')
        self.assertEqual(character_form('鲸鱼娘表情包'), 'chibi')
        with self.assertRaises(ValueError):
            character_form('DS娘', '../x')

    def test_text_card_without_reference_does_not_claim_image_attachment(self):
        self.assertTrue(character_requested('鲸鱼娘Q版微笑'))
        self.assertFalse(character_requested('画一条蓝色大肥鱼'))
        prompt = anchored_prompt('DS娘Q版微笑', None)
        self.assertIn('本群未配置固定原图', prompt)
        self.assertNotIn('第None张', prompt)

    def test_appearance_override_requires_exact_human_quote_in_same_group(self):
        message = {'session_id': 'group-a', 'message_info': {'user_info': {'user_id': '123'}},
                   'raw_message': [{'type': 'text', 'data': '把DS娘染成紫色短发，换成红裙'}]}
        quote = verified_appearance_change(message, 'group-a', '染成紫色短发')
        self.assertIn('【核实后的用户外观修改】染成紫色短发', anchored_prompt('DS娘微笑', 1, quote))
        for session, text in (('group-b', '染成紫色短发'), ('group-a', '变成猫娘'), ('group-a', '微笑')):
            with self.assertRaises(ValueError):
                verified_appearance_change(message, session, text)
        message['message_info']['user_info']['user_id'] = '3976405558'
        with self.assertRaises(ValueError):
            verified_appearance_change(message, 'group-a', '染成紫色短发')


if __name__ == '__main__':
    unittest.main()

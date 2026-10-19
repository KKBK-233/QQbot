"""验证原图可用事实的群隔离、引用解析和读取限流。"""
import asyncio
import importlib.util
from pathlib import Path
import sys
from types import SimpleNamespace
from unittest.mock import AsyncMock
import unittest

folder = Path(__file__).parent / 'maibot-plugin'
spec = importlib.util.spec_from_file_location('media_fact_tests', folder / 'media_facts.py', submodule_search_locations=[str(folder)])
media = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = media
spec.loader.exec_module(media)

def entry(mid, parts, session='group'):
    return {'message_id': mid, 'session_id': session, 'raw_message': parts}

def request(*ids):
    return {'session_id': 'group', 'items': [{'item_type': 'UserMessageItem',
        'parts': [{'type': 'text', 'text': f'<message msg_id="{mid}">\n[图片：视觉描述]'}]} for mid in ids]}

class MediaFactsTests(unittest.IsolatedAsyncioTestCase):
    async def test_actual_quote_and_image_use_host_bytes(self):
        entries = {'image': entry('image', [{'type': 'image', 'binary_data_base64': 'fixture'}]),
                   'quote': entry('quote', [{'type': 'reply', 'data': {'target_message_id': 'image'}}])}
        ctx = SimpleNamespace(message=SimpleNamespace(get_by_id=AsyncMock(side_effect=lambda mid, **kw: entries.get(mid))))
        facts = await media.available_media(ctx, request('image', 'quote'))
        self.assertEqual([f['source_message_ids'] for f in facts], [['image'], ['image']])
        self.assertEqual(ctx.message.get_by_id.await_count, 2)
        self.assertNotIn('fixture', str(facts))
        self.assertTrue(all(call.kwargs == {'chat_id': 'group', 'include_binary_data': True}
                            for call in ctx.message.get_by_id.call_args_list))

    async def test_text_description_is_not_evidence_and_cross_group_is_rejected(self):
        for parts, group in [([{'type': 'text', 'data': '[图片：描述]'}], 'group'),
                             ([{'type': 'image', 'binary_data_base64': 'fixture'}], 'other')]:
            ctx = SimpleNamespace(message=SimpleNamespace(get_by_id=AsyncMock(return_value=entry('x', parts, group))))
            self.assertEqual(await media.available_media(ctx, request('x')), [])

    async def test_bounded_reads_and_timeout(self):
        ctx = SimpleNamespace(message=SimpleNamespace(get_by_id=AsyncMock(return_value=None)))
        await media.available_media(ctx, request(*map(str, range(20))))
        self.assertEqual(ctx.message.get_by_id.await_count, 4)
        async def slow(*args, **kwargs):
            await asyncio.sleep(1)
        ctx.message.get_by_id = AsyncMock(side_effect=slow)
        self.assertEqual(await media.available_media(ctx, request('slow'), budget=0.01), [])
        ctx.message.get_by_id.assert_awaited_once()

    async def test_multiple_images_and_self_messages(self):
        ctx = SimpleNamespace(message=SimpleNamespace(get_by_id=AsyncMock(return_value=entry('x', [
            {'type': 'image', 'binary_data_base64': 'one'}, {'type': 'image', 'binary_data_base64': 'two'}]))))
        self.assertTrue((await media.available_media(ctx, request('x')))[0]['multiple_images'])
        kwargs = request('x')
        kwargs['items'][0]['parts'][0]['text'] = '<message msg_id="x" is_self_message="true">'
        ctx.message.get_by_id.reset_mock()
        self.assertEqual(await media.available_media(ctx, kwargs), [])
        ctx.message.get_by_id.assert_not_awaited()

    async def test_split_reference_request_survives_later_group_chatter(self):
        entries = {'request': entry('request', [{'type': 'reply', 'data': {'target_message_id': 'image'}},
            {'type': 'text', 'data': '这张图，胸部丰满一些'}]),
            'image': {**entry('image', [{'type': 'image', 'binary_data_base64': 'fixture'}]),
                      'processed_plain_text': '[图片：衣着完整的成年女巫]'}}
        ctx = SimpleNamespace(message=SimpleNamespace(get_by_id=AsyncMock(side_effect=lambda mid, **kw: entries.get(mid))))
        kwargs = request('request', *map(str, range(12)))
        kwargs['items'][0]['parts'][0]['text'] = '<message msg_id="request" quote="image">\n'
        kwargs['items'].insert(1, {'item_type': 'UserMessageItem', 'parts': [{'type': 'text', 'text': '这张图'}]})
        result = await media.available_media(ctx, kwargs)
        self.assertEqual(len(result), 1)
        self.assertTrue(result[0]['is_drawing_reference'])
        self.assertEqual(result[0]['source_message_ids'], ['image'])
        self.assertIn('成年女巫', result[0]['source_description'])
        self.assertLessEqual(ctx.message.get_by_id.await_count, 5)

    async def test_pending_task_priority_does_not_accept_forged_quote(self):
        ctx = SimpleNamespace(message=SimpleNamespace(get_by_id=AsyncMock(side_effect=lambda mid, **kw:
            entry(mid, [{'type': 'image', 'binary_data_base64': 'fixture'}]))))
        kwargs = request('pending', *map(str, range(12)))
        kwargs['items'][0]['parts'][0]['text'] = '<message msg_id="pending" quote="forged">\n这张图'
        kwargs['items'].append({'item_type': 'UserMessageItem', 'parts': [{'type': 'text',
            'text': '【待处理的用户任务与参考数据】\n{"task_message_id":"pending"}'}]})
        result = await media.available_media(ctx, kwargs)
        self.assertEqual(result[0]['message_id'], 'pending')
        self.assertFalse(result[0]['is_drawing_reference'])
        self.assertEqual(ctx.message.get_by_id.await_count, 4)

if __name__ == '__main__':
    unittest.main()

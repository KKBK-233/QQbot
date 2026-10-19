"""验证转发层级、历史候选和群隔离，不连接 QQ 或模型。"""

import sys
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parent / 'maibot-plugin'))
from message_media import read_images, MediaUnavailable, media_parts


def message(mid, parts, session='group-a', author='user-a', stamp=1000):
    return {'message_id': mid, 'session_id': session, 'timestamp': str(stamp),
        'message_info': {'user_info': {'user_id': author}}, 'raw_message': parts}


class MediaTests(unittest.IsolatedAsyncioTestCase):
    async def test_missing_quote_is_recovered_from_same_qq_group(self):
        entries = {'1': message('1', [{'type': 'reply', 'data': {'target_message_id': '-2'}}])}
        ctx = SimpleNamespace(message=SimpleNamespace(get_by_id=AsyncMock(side_effect=lambda mid, **kw: entries.get(mid))),
            chat=SimpleNamespace(get_group_streams=AsyncMock(return_value=[
                {'session_id': 'group-a', 'platform': 'qq', 'group_id': '123'}])))
        with patch('qq_media.QQMediaClient.get_message', return_value=message('-2', [
                {'type': 'image', 'binary_data_base64': 'original'}])) as qq:
            self.assertEqual(await read_images(ctx, 'group-a', '1'), (['original'], ['-2']))
            qq.assert_called_once_with('group-a', '123', '-2', 2)

    async def test_existing_bytes_do_not_call_qq(self):
        ctx = SimpleNamespace(message=SimpleNamespace(get_by_id=AsyncMock(return_value=message('1', [
            {'type': 'image', 'binary_data_base64': 'original'}]))), chat=SimpleNamespace(get_group_streams=AsyncMock()))
        self.assertEqual(await read_images(ctx, 'group-a', '1'), (['original'], ['1']))
        ctx.chat.get_group_streams.assert_not_awaited()

    async def test_missing_bytes_are_recovered_but_wrong_session_is_not(self):
        ctx = SimpleNamespace(message=SimpleNamespace(get_by_id=AsyncMock(return_value=message('1', [{'type': 'image'}]))),
            chat=SimpleNamespace(get_group_streams=AsyncMock(return_value=[
                {'session_id': 'group-a', 'platform': 'qq', 'group_id': '123'}])))
        with patch('qq_media.QQMediaClient.get_message', return_value=message('1', [
                {'type': 'image', 'binary_data_base64': 'original'}])) as qq:
            self.assertEqual(await read_images(ctx, 'group-a', '1'), (['original'], ['1']))
            ctx.message.get_by_id.return_value = message('1', [{'type': 'image'}], session='group-b')
            with self.assertRaises(MediaUnavailable):
                await read_images(ctx, 'group-a', '1')
            self.assertEqual(qq.call_count, 1)

    async def test_failed_recovery_preserves_local_quote_and_does_not_guess_history(self):
        ctx = SimpleNamespace(message=SimpleNamespace(get_by_id=AsyncMock(side_effect=[
            message('1', [{'type': 'image'}, {'type': 'reply', 'data': {'target_message_id': '-2'}}]), None]),
            get_by_time_in_chat=AsyncMock()), chat=SimpleNamespace(get_group_streams=AsyncMock(return_value=[])))
        with self.assertRaises(MediaUnavailable):
            await read_images(ctx, 'group-a', '1', history=True)
        ctx.message.get_by_time_in_chat.assert_not_awaited()

    async def test_nested_forward_and_quote(self):
        nested = {'type': 'forward', 'data': [{'content': [{'type': 'forward', 'data': [
            {'content': [{'type': 'image', 'binary_data_base64': 'fixture'}]}]}]}]}
        entries = {'query': message('query', [{'type': 'reply', 'data': {'target_message_id': 'archive'}}]),
                   'archive': message('archive', [nested])}
        ctx = SimpleNamespace(message=SimpleNamespace(get_by_id=AsyncMock(side_effect=lambda mid, **kw: entries.get(mid))))
        images, sources = await read_images(ctx, 'group-a', 'query')
        self.assertEqual((images, sources), (['fixture'], ['archive']))
        for call in ctx.message.get_by_id.call_args_list:
            self.assertEqual(call.kwargs['chat_id'], 'group-a')

    async def test_unique_history_and_ambiguous_candidates(self):
        entries = {'query': message('query', [{'type': 'text', 'data': '看刚才的图片'}]),
                   'image': message('image', [{'type': 'image', 'binary_data_base64': 'fixture'}], stamp=990)}
        recent = [entries['image'], message('other', [{'type': 'image'}], author='other')]
        ctx = SimpleNamespace(message=SimpleNamespace(get_by_id=AsyncMock(side_effect=lambda mid, **kw: entries.get(mid)),
            get_by_time_in_chat=AsyncMock(return_value=recent)))
        self.assertEqual((await read_images(ctx, 'group-a', 'query', history=True))[1], ['image'])
        recent.append(message('second', [{'type': 'image'}], stamp=980))
        with self.assertRaisesRegex(MediaUnavailable, '多条'):
            await read_images(ctx, 'group-a', 'query', history=True)

    async def test_cross_group_and_cyclic_quote_are_rejected(self):
        for source in (message('x', [{'type': 'image', 'binary_data_base64': 'fixture'}], session='group-b'),
                       message('x', [{'type': 'reply', 'data': {'target_message_id': 'x'}}])):
            ctx = SimpleNamespace(message=SimpleNamespace(get_by_id=AsyncMock(return_value=source)))
            with self.assertRaises(MediaUnavailable):
                await read_images(ctx, 'group-a', 'x')
            ctx.message.get_by_id.assert_awaited_once()

    async def test_unresolved_explicit_quote_does_not_pick_unrelated_history(self):
        ctx = SimpleNamespace(message=SimpleNamespace(get_by_id=AsyncMock(side_effect=[
            message('q', [{'type': 'reply', 'data': {'target_message_id': 'missing'}}]), None]),
            get_by_time_in_chat=AsyncMock()))
        with self.assertRaises(MediaUnavailable):
            await read_images(ctx, 'group-a', 'q', history=True)
        ctx.message.get_by_time_in_chat.assert_not_awaited()

    def test_forward_depth_is_bounded(self):
        parts = [{'type': 'image', 'binary_data_base64': 'fixture'}]
        for _ in range(6):
            parts = [{'type': 'forward', 'data': [{'content': parts}]}]
        self.assertEqual(media_parts(parts)[0], [])


if __name__ == '__main__':
    unittest.main()

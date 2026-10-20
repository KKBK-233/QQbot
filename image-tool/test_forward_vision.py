"""转发选图用真实原图序号，不把占位符或前一批的图当作当前图片。"""

from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock
import asyncio
import importlib.util

import pytest

path = Path(__file__).parent / 'maibot-plugin/forward_vision.py'
spec = importlib.util.spec_from_file_location('forward_vision_tested', path)
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)


def context(parts, session='group-a'):
    return SimpleNamespace(message=SimpleNamespace(get_by_id=AsyncMock(return_value={
        'session_id': session, 'raw_message': parts})),
        llm=SimpleNamespace(generate=AsyncMock(return_value={'success': True, 'response': '本批图片内容'})))


def images():
    return [{'type': 'image', 'binary_data_base64': str(n)} for n in range(1, 6)]


def test_nested_forward_reads_selected_third_and_fourth_images():
    parts = [{'type': 'forward', 'data': [{'content': images()[:2]}, {'content': [
        {'type': 'forward', 'data': [{'content': images()[2:]}]}]}]}]
    ctx = context(parts)
    result = asyncio.run(module.analyze_forward_images(ctx, 'group-a', 'outer', [3, 4], lambda s: 'data:' + s, '识别公告'))
    assert result['success'] and result['image_numbers'] == [3, 4]
    assert result['source_message_ids'] == ['outer']
    content = ctx.llm.generate.await_args.args[0][-1]['content']
    assert [p['image_url']['url'] for p in content if p['type'] == 'image_url'] == ['data:3', 'data:4']
    assert any(p.get('text') == '下面是原记录的图片 3：' for p in content)


def test_default_forward_batch_exposes_remaining_numbers():
    ctx = context([{'type': 'forward', 'data': [{'content': images()}]}])
    assert asyncio.run(module.has_forward(ctx, 'group-a', 'outer'))
    result = asyncio.run(module.analyze_forward_images(ctx, 'group-a', 'outer', None, lambda s: s, ''))
    assert result['success'] and result['image_numbers'] == [1, 2, 3, 4]
    assert result['total_image_count'] == 5 and result['remaining_image_numbers'] == [5]


@pytest.mark.parametrize('numbers', [[0], [1, 1], [1, 2, 3, 4, 5], ['1'], [], [True]])
def test_invalid_selection_does_not_read_or_call_model(numbers):
    ctx = context(images())
    result = asyncio.run(module.analyze_forward_images(ctx, 'group-a', 'outer', numbers, lambda s: s, ''))
    assert not result['success']
    ctx.message.get_by_id.assert_not_awaited()
    ctx.llm.generate.assert_not_awaited()


def test_missing_image_is_not_replaced_by_another_and_cross_group_is_rejected():
    for parts, session in [([{'type': 'image'}, *images()], 'group-a'), (images(), 'group-b')]:
        ctx = context(parts, session)
        result = asyncio.run(module.analyze_forward_images(ctx, 'group-a', 'outer', [1], lambda s: s, ''))
        assert not result['success']
        ctx.llm.generate.assert_not_awaited()


def test_quote_uses_original_message_and_out_of_range_is_explicit():
    ctx = context([])
    ctx.message.get_by_id.side_effect = [
        {'session_id': 'group-a', 'raw_message': [{'type': 'reply', 'data': {'target_message_id': 'original'}}]},
        {'session_id': 'group-a', 'raw_message': images()}]
    result = asyncio.run(module.analyze_forward_images(ctx, 'group-a', 'request', [5], lambda s: s, ''))
    assert result['success'] and result['source_message_ids'] == ['original']
    ctx = context(images())
    result = asyncio.run(module.analyze_forward_images(ctx, 'group-a', 'outer', [6], lambda s: s, ''))
    assert not result['success'] and '5 张' in result['content']
    ctx.llm.generate.assert_not_awaited()

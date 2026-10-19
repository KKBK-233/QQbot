"""回放实际历史图片及后续文字请求，不调用视觉模型或发送群消息。"""

import asyncio
from contextlib import redirect_stdout, redirect_stderr
import io
import json
import os
from pathlib import Path
import sys
from types import SimpleNamespace
from unittest.mock import AsyncMock

folder = Path(__file__).resolve().parent
sys.path.insert(0, str(folder.parent / 'MaiBot/plugins/qqbot-task-assist'))
from task_flow import TaskFlow, PendingTask


async def replay():
    """宿主数据库只读，视觉调用用替身验证原图确实进入请求。"""
    os.chdir('/home/qqbot/QQbot/MaiBot')
    sys.path.insert(0, os.getcwd())
    from src.services import message_service
    from src.plugin_runtime.host.message_utils import PluginMessageUtils
    session = 'eea9ec46d0880b86983de2e4235f4bca'

    async def get_message(mid, chat_id='', include_binary_data=False):
        msg = message_service.get_message_by_id(mid, chat_id=chat_id)
        return PluginMessageUtils._session_message_to_dict(msg, include_binary_data=include_binary_data) if msg else None

    async def recent(chat_id, start_time, end_time, limit=40, include_binary_data=False):
        messages = message_service.get_messages_by_time_in_chat(chat_id=chat_id, start_time=start_time, end_time=end_time, limit=limit)
        return [PluginMessageUtils._session_message_to_dict(m, include_binary_data=include_binary_data) for m in messages]

    async def fake_visual(prompt, **kwargs):
        images = [p for p in prompt[-1]['content'] if p.get('type') == 'image_url']
        assert images and images[0]['image_url']['url'].startswith('data:image/jpeg;base64,')
        return {'success': True, 'response': '离线替身：原图已传入视觉请求。'}

    ctx = SimpleNamespace(message=SimpleNamespace(get_by_id=get_message, get_by_time_in_chat=recent),
        llm=SimpleNamespace(generate=AsyncMock(side_effect=fake_visual)), logger=SimpleNamespace(warning=lambda *args: None))
    flow = TaskFlow()
    # 历史图片无需当前待处理任务，之前这次调用错误地返回“任务过期”。
    standalone = await flow.analyze(ctx, session, '-328853748')
    assert standalone['success'], standalone
    # 后续文字请求存在多个候选时明确返回编号，不冒认另一张图片。
    flow.pending[session] = PendingTask('1284376833', '看之前图片并提取人物特征', 0, True)
    history = await flow.analyze(ctx, session, '1284376833')
    assert not history['success'] and '候选消息编号' in history['content'], history
    assert '-328853748' in history['content'], history
    selected = await flow.analyze(ctx, session, '-328853748')
    assert selected['success'], selected
    forwarded = await flow.analyze(ctx, session, '-152159406')
    assert forwarded['success'] and forwarded['image_count'] == 2, forwarded
    from task_flow import tool_item
    flow.pending[session] = PendingTask('current', '提取之前图片的特征', 0, True)
    flow.tools[session] = {'reply', 'analyze_task_image'}
    calls = {'session_id': session, 'output_items': [tool_item('analyze_task_image', {'message_id': '-328853748'})]}
    assert flow.after(calls) == calls, '明确历史目标被替换成文字消息'
    return {'历史图片独立读取': standalone['success'], '多图候选明确列出': history['content'], '明确历史目标保留': True,
            '实际合并转发原图': forwarded['image_count'],
            '真实视觉请求': 0, 'QQ发送': 0}


if __name__ == '__main__':
    with redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()):
        result = asyncio.run(replay())
    print(json.dumps(result, ensure_ascii=False))

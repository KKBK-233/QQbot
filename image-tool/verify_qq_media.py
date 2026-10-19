"""只读回放本地缺失的真实引用图片，不调用模型或发送群消息。"""
import asyncio
import base64
from contextlib import redirect_stdout, redirect_stderr
import hashlib
import io
import json
import os
from pathlib import Path
import sys
import time
from types import SimpleNamespace


async def replay():
    os.chdir('/home/qqbot/QQbot/MaiBot')
    sys.path.insert(0, os.getcwd())
    from src.services import message_service
    from src.plugin_runtime.host.message_utils import PluginMessageUtils
    from src.chat.message_receive.chat_manager import chat_manager
    from PIL import Image
    # 使用部署中的模块验证，不靠测试目录中的实现替代生产代码。
    sys.path.insert(0, '/home/qqbot/QQbot/MaiBot/plugins/qqbot-image')
    from message_media import read_images, MediaUnavailable
    session = 'eea9ec46d0880b86983de2e4235f4bca'

    async def get(mid, chat_id='', include_binary_data=False):
        msg = message_service.get_message_by_id(mid, chat_id=chat_id)
        return PluginMessageUtils._session_message_to_dict(msg, include_binary_data=include_binary_data) if msg else None

    async def groups(**kwargs):
        return [{'session_id': s.session_id, 'platform': s.platform, 'group_id': s.group_id}
                for s in chat_manager.sessions.values() if s.platform == 'qq' and s.is_group_session]

    await chat_manager.load_all_sessions_from_db()
    ctx = SimpleNamespace(message=SimpleNamespace(get_by_id=get), chat=SimpleNamespace(get_group_streams=groups))
    assert await get('-475107884', chat_id=session, include_binary_data=True) is None
    start = time.monotonic()
    images, sources = await asyncio.wait_for(read_images(ctx, session, '-2058144179'), 1.5)
    elapsed = time.monotonic() - start
    assert sources == ['-475107884'] and len(images) == 1
    data = base64.b64decode(images[0], validate=True)
    with Image.open(io.BytesIO(data)) as image:
        dimensions = image.size
        image.verify()
    original = Path('/mnt/c/Users/668/Downloads/泳池暖阳下的德克萨斯.png').read_bytes()
    assert data == original
    # 检查规划器事实入口和原有目标绑定，两者使用补取后也能认出真实引用。
    import importlib.util
    spec = importlib.util.spec_from_file_location('qq_media_verify',
        '/home/qqbot/QQbot/MaiBot/plugins/qqbot-image/media_facts.py',
        submodule_search_locations=['/home/qqbot/QQbot/MaiBot/plugins/qqbot-image'])
    media = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = media
    spec.loader.exec_module(media)
    facts = await media.available_media(ctx, {'session_id': session, 'items': [
        {'item_type': 'UserMessageItem', 'parts': [{'type': 'text',
            'text': '<message msg_id="-2058144179" quote="-475107884">\n换成ds娘'}]}]})
    assert facts and facts[0]['source_message_ids'] == sources and facts[0]['original_available']
    sys.path.insert(0, '/home/qqbot/QQbot/MaiBot/plugins/qqbot-task-assist')
    from visual_target import bind_visual_target, target_matches
    task = SimpleNamespace(message_id='-2058144179', created=0)
    await bind_visual_target(ctx, session, task, await get(task.message_id, chat_id=session), is_edit=True)
    assert task.image_source_message_ids == sources and target_matches(task, '-475107884')
    # 同一消息在其他真实群的会话中不能补取。
    other = next(s['session_id'] for s in await groups() if s['session_id'] != session)
    try:
        await read_images(ctx, other, '-475107884')
    except MediaUnavailable:
        pass
    else:
        raise AssertionError('跨群补取未被阻止')
    return {'本地引用目标缺失': True, 'QQ补取原图成功': True, 'source_message_ids': sources,
            '字节数': len(data), '尺寸': dimensions, '与用户原图字节一致': True,
            'sha256': hashlib.sha256(data).hexdigest(), '补取耗时秒': round(elapsed, 3),
            '规划器原图事实正确': True, '原有目标绑定正确': True,
            '跨群补取已拒绝': True, '模型调用': 0, '群发送': 0}


if __name__ == '__main__':
    with redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()):
        result = asyncio.run(replay())
    print(json.dumps(result, ensure_ascii=False))

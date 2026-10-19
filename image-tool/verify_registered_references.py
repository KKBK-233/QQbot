"""核对已注册普通版与Q版的真实群原图及提交参数，不生图、不发送QQ消息。"""

import asyncio
import base64
from contextlib import redirect_stdout, redirect_stderr
import hashlib
import importlib.util
import io
import json
import os
from pathlib import Path
import sys
import tempfile
from types import SimpleNamespace
from unittest.mock import AsyncMock


async def verify():
    """复用宿主消息序列化和实际媒体读取，后端仅用替身记录最终请求。"""
    os.chdir('/home/qqbot/QQbot/MaiBot')
    sys.path.insert(0, os.getcwd())
    from src.services import message_service
    from src.plugin_runtime.host.message_utils import PluginMessageUtils
    folder = Path(__file__).resolve().parent / 'maibot-plugin'
    spec = importlib.util.spec_from_file_location('registered_image_adapter', folder / 'plugin.py',
                                                  submodule_search_locations=[str(folder)])
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    media = __import__(spec.name + '.message_media', fromlist=['media_parts'])

    async def get_message(mid, chat_id='', include_binary_data=False):
        message = message_service.get_message_by_id(mid, chat_id=chat_id)
        return PluginMessageUtils._session_message_to_dict(message, include_binary_data=include_binary_data) if message else None

    plugin = module.create_plugin()
    plugin._ctx = SimpleNamespace(message=SimpleNamespace(get_by_id=get_message))
    plugin.client = SimpleNamespace(call=AsyncMock(return_value={'content': [
        {'type': 'text', 'text': json.dumps({'state': 'queued'})}]}))
    plugin.tasks = {}
    temporary = tempfile.TemporaryDirectory()
    plugin.memories = module.CharacterMemory(Path(temporary.name) / 'character_memory')
    plugin.deliver = AsyncMock()
    session = 'eea9ec46d0880b86983de2e4235f4bca'
    checked = []
    for form, mid in [('standard', '-328853748'), ('chibi', '-1178010614')]:
        original = await get_message(mid, chat_id=session, include_binary_data=True)
        assert original and original['session_id'] == session, '已注册原图不可读：' + form
        images, _ = media.media_parts(original['raw_message'])
        assert len(images) == 1, '参考必须是单张完整图'
        result = await plugin.image('generate', 'verify-ds-' + form, 'DS娘微笑，紫色短发，紫色背景',
                                    form=form, character='deepseek', stream_id=session)
        assert result['success'], result
        submitted = plugin.client.call.await_args.args[0]
        assert submitted['action'] == 'edit'
        assert len(submitted['references']) == 1
        data = Path('/mnt/d/code/web/QQbot/data/image-tool/inbox', submitted['references'][0]).read_bytes()
        assert hashlib.sha256(data).digest() == hashlib.sha256(base64.b64decode(images[0])).digest()
        assert '紫色短发' not in submitted['prompt'] and '紫色背景' in submitted['prompt']
        if form == 'chibi':
            assert '2.5头身' in submitted['prompt'] and '麻花辫' not in submitted['prompt']
        else:
            assert '长麻花辫' in submitted['prompt']
        await asyncio.gather(*plugin.tasks.values())
        checked.append({'form': form, 'reference_message_id': mid, 'bytes': len(data), 'action': submitted['action']})
    temporary.cleanup()
    return {'registered_references': checked, '真实生图': 0, 'QQ发送': 0}


if __name__ == '__main__':
    with redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()):
        result = asyncio.run(verify())
    print(json.dumps(result, ensure_ascii=False))

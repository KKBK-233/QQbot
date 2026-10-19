"""回放真实改图消息，验证路由和原图导入；不调用模型或发送 QQ 消息。"""

import asyncio
from contextlib import redirect_stdout, redirect_stderr
import importlib.util
import io
import json
import os
from pathlib import Path
import sys

folder = Path(__file__).resolve().parent
sys.path.insert(0, str(folder.parent / 'MaiBot/plugins/qqbot-task-assist'))
from task_flow import TaskFlow, text_item, tool_item

spec = importlib.util.spec_from_file_location('image_edit_probe', folder / 'maibot-plugin/plugin.py', submodule_search_locations=[str(folder / 'maibot-plugin')])
module = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = module
spec.loader.exec_module(module)


async def main():
    """实际宿主数据库只读，原图复制到隔离的测试编号用于核对。"""
    os.chdir('/home/qqbot/QQbot/MaiBot')
    sys.path.insert(0, os.getcwd())
    from src.services import message_service
    from src.plugin_runtime.host.message_utils import PluginMessageUtils
    from types import SimpleNamespace
    session = 'eea9ec46d0880b86983de2e4235f4bca'

    async def get_message(mid, chat_id='', include_binary_data=False):
        msg = message_service.get_message_by_id(mid, chat_id=chat_id)
        return PluginMessageUtils._session_message_to_dict(msg, include_binary_data=include_binary_data) if msg else None

    ctx = SimpleNamespace(message=SimpleNamespace(get_by_id=get_message))
    original = await get_message('-1723457829', chat_id=session)
    now = float(original['timestamp']) + 1
    flow = TaskFlow()
    flow.started = now - 10
    kwargs = {'session_id': session, 'items': [text_item('UserMessageItem', '<message msg_id="-1723457829">\n帮我改改这张图呗：P')],
              'tool_definitions': [{'type': 'function', 'function': {'name': name}} for name in ['reply', 'analyze_task_image', 'chatgpt_image']]}
    prepared = await flow.before(ctx, kwargs, now)
    assert not flow.pending[session].visual, '改图仍被强制路由识图'
    assert 'IMAGE_EDIT_STYLE' not in str(prepared) and 'chatgpt_image' in str(prepared)
    calls = {'session_id': session, 'output_items': [tool_item('chatgpt_image', {'action': 'edit', 'message_id': '-1333653234'})]}
    assert flow.after(calls) == calls, '编辑工具被替换'
    # 反推任务仍走原有识图流程。
    from task_flow import PendingTask
    flow.pending[session] = PendingTask('reverse', '反推这张图片的提示词', now, True)
    assert flow.after({'session_id': session, 'output_items': []})['output_items'][0]['tool_call']['func_name'] == 'analyze_task_image'
    plugin = module.create_plugin()
    plugin._ctx = ctx
    name = await plugin.import_reference(session, '-1333653234', 'probe-edit-reference')
    imported = Path('/mnt/d/code/web/QQbot/data/image-tool/inbox') / name
    assert imported.stat().st_size > 100
    # 只用固定测试文件名，核对后移除，绝不删除用户原图。
    size = imported.stat().st_size
    imported.unlink()
    return {'edit_routing': 'passed', 'image_bytes': size, 'reverse_prompt_routing': 'passed', 'generation_requests': 0}


if __name__ == '__main__':
    with redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()):
        result = asyncio.run(main())
    print(json.dumps(result))

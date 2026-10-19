"""离线验证搜索插件 Hook 与主程序回复参考格式，不发送群消息或调用模型。"""

from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path

import asyncio
import importlib.util
import io
import os
import sys

ROOT = Path('/home/qqbot/QQbot/MaiBot')
os.chdir(ROOT)
sys.path.insert(0, str(ROOT))
# 初始化日志只在内存中捕获，避免将凭据相关日志打印到验证输出。
with redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()):
    spec = importlib.util.spec_from_file_location('search_probe', ROOT / 'plugins/qqbot-web-search/plugin.py',
                                                submodule_search_locations=[str(ROOT / 'plugins/qqbot-web-search')])
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    plugin = module.create_plugin()
    asyncio.run(plugin.on_load())
    components = plugin.get_components()
    assert any('inject_search_reference' in str(component) for component in components)
    assert any('read_webpage' in str(component) for component in components)
    sources = ['https://example.com/one', 'https://example.com/two', 'https://example.com/three']
    result = {'success': True, 'provider': 'DuckDuckGo', 'results': [
        {'title': '模型', 'snippet': '搜索摘要', 'url': url} for url in sources]}
    plugin.reply_bridge.remember('offline-session', '人脸识别小模型', result)
    hook = asyncio.run(plugin.inject_search_reference(
        session_id='offline-session', reply_tool_args={'reply_reference': 'Planner 概要', 'reply_style': '简短表达'}))
    args = hook['modified_kwargs']['reply_tool_args']
    from src.chat.replyer.maisaka_generator_base import BaseMaisakaReplyGenerator
    reference = BaseMaisakaReplyGenerator._build_reply_reference_message('查找模型', args['reply_reference'])
    assert all(url in reference for url in sources)
    assert args['reply_style'] == '正常回复'
    assert '日常聊天能一句说完就一句' in hook['modified_kwargs']['extra_prompt']
    ordinary_hook = asyncio.run(plugin.inject_search_reference(session_id='offline-other', reply_tool_args={}))
    assert '日常聊天能一句说完就一句' in ordinary_hook['modified_kwargs']['extra_prompt']
    asyncio.run(plugin.on_unload())
print('SDK Hook 组件已识别；主程序回复参考保留3个来源；无需发送群消息。')

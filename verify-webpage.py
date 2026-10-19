"""验证真实公开网页读取、沙箱和回复参考传递，不调用模型、不发送群消息。"""

from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from types import SimpleNamespace

import asyncio
import importlib.util
import io
import json
import os
import sys
import argparse

ROOT = Path('/home/qqbot/QQbot/MaiBot')
os.chdir(ROOT)
sys.path.insert(0, str(ROOT))


async def verify(url, mode):
    """经插件的正式工具入口读取官方文档，并验证同会话原文进入回复参考。"""
    source = ROOT / 'plugins/qqbot-web-search'
    spec = importlib.util.spec_from_file_location('webpage_probe', source / 'plugin.py', submodule_search_locations=[str(source)])
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    plugin = module.create_plugin()
    await plugin.on_load()
    # 仅使用公开代理地址，不读取或打印模型配置中的凭据。
    plugin._plugin_config_instance = SimpleNamespace(search=SimpleNamespace(proxy='http://127.0.0.1:2081'))
    page = await plugin.handle_read_webpage(url, mode=mode, stream_id='webpage-offline-check')
    assert page['success'], page['content']
    assert len(page['text']) <= 10000
    assert len(page['text'].strip()) > 10
    hook = await plugin.inject_search_reference(session_id='webpage-offline-check', reply_tool_args={})
    reference = hook['modified_kwargs']['reply_tool_args']['reply_reference']
    assert page['url'] in reference
    data = json.loads(reference.split('\n', 1)[1])
    assert data['results'][0]['snippet'] == page['text']
    from src.chat.replyer.maisaka_generator_base import BaseMaisakaReplyGenerator
    built = BaseMaisakaReplyGenerator._build_reply_reference_message('', reference)
    assert page['url'] in built
    blocked = await plugin.handle_read_webpage('http://127.0.0.1:8001/', stream_id='webpage-denied-check')
    assert not blocked['success']
    await plugin.on_unload()
    return {'public_page': page['url'], 'provider': page['provider'], 'mode': mode, 'title': page['title'], 'text_length': len(page['text']),
            'truncated': page['truncated'], 'reply_reference': '完整保留', 'local_endpoint': '访问前拒绝'}


parser = argparse.ArgumentParser(description=__doc__)
parser.add_argument('--url', default='https://docs.python.org/3/library/asyncio.html')
parser.add_argument('--mode', choices=['auto', 'static', 'browser'], default='auto')
args = parser.parse_args()
with redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()):
    result = asyncio.run(verify(args.url, args.mode))
print(json.dumps(result, ensure_ascii=False, indent=2))

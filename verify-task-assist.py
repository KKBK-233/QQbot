"""回放已知任务，验证宿主消息查询、图片引用、Hook 协议；不发送群消息。"""

from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from types import SimpleNamespace

import asyncio
import importlib.util
import io
import json
import logging
import os
import sys

ROOT = Path('/home/qqbot/QQbot/MaiBot')
SOURCE = Path('/mnt/d/code/web/QQbot/MaiBot/plugins/qqbot-task-assist')
os.chdir(ROOT)
sys.path.insert(0, str(ROOT))
capture = io.StringIO()


async def replay():
    """本地只查询原消息；--live 时额外执行一次视觉模型和一次回复模型。"""
    spec = importlib.util.spec_from_file_location('task_probe', SOURCE / 'plugin.py', submodule_search_locations=[str(SOURCE)])
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    plugin = module.create_plugin()
    await plugin.on_load()
    assert len(plugin.get_components()) == 7
    from src.services import message_service, llm_service
    from src.plugin_runtime.host.message_utils import PluginMessageUtils
    from src.plugin_runtime.hook_payloads import deserialize_prompt_items
    from src.llm_models.payload_content.context_item import CONTEXT_ITEM_SCHEMA_VERSION
    from src.llm_models.payload_content.context_protocol import ContextProtocolMode

    async def get_message(message_id, chat_id='', include_binary_data=False):
        def read():
            message = message_service.get_message_by_id(message_id, chat_id=chat_id)
            return PluginMessageUtils._session_message_to_dict(message, include_binary_data=include_binary_data) if message else None
        return await asyncio.to_thread(read)

    seen_images = []

    async def generate(prompt, **kwargs):
        parts = prompt[-1]['content']
        images = [part for part in parts if part.get('type') == 'image_url']
        assert images, '识图请求必须包含原图'
        seen_images.extend(images)
        if '--live' in sys.argv:
            result = await llm_service.generate(llm_service.LLMServiceRequest(prompt=prompt, request_type='plugin.task_assist_probe', **kwargs))
            return result.to_capability_payload()
        return {'success': True, 'response': '离线视觉替身：已确认原图送入请求。'}

    ctx = SimpleNamespace(message=SimpleNamespace(get_by_id=get_message), llm=SimpleNamespace(generate=generate), logger=logging.getLogger('task_probe'))
    mention = '--mention' in sys.argv
    message_id = '-1276932882' if mention else '96866415'
    snapshot_name = '1790863889617' if mention else '1790860681542'
    snapshot = json.loads((ROOT / 'logs/maisaka_prompt/planner/qq_group_1022382380' / (snapshot_name + '.json')).read_text())
    session = 'eea9ec46d0880b86983de2e4235f4bca'
    original = await get_message(message_id, chat_id=session)
    assert original
    now = float(original['timestamp']) + 1
    plugin.flow.started = now - 10
    kwargs = {'session_id': session, 'items': snapshot['request_items'],
        'tool_definitions': [{'type': 'function', 'function': {'name': name}} for name in ['reply', 'wait', 'analyze_task_image']]}
    prepared = await plugin.flow.before(ctx, kwargs, now=now)
    assert plugin.flow.pending[session].message_id == message_id
    deserialize_prompt_items(prepared['items'], item_schema_version=CONTEXT_ITEM_SCHEMA_VERSION, mode=ContextProtocolMode.REQUEST_CONTEXT)
    routed = plugin.flow.after({'session_id': session, 'output_items': snapshot['output_items']})
    if mention:
        assert routed['output_items'][0]['tool_call']['func_name'] == 'reply'
        deserialize_prompt_items(routed['output_items'], item_schema_version=CONTEXT_ITEM_SCHEMA_VERSION, mode=ContextProtocolMode.MODEL_OUTPUT)
        reply = plugin.flow.reply_before({'session_id': session, 'reply_message_id': message_id, 'reply_tool_args': {}})
        result = {'原消息': message_id, '宿主确认被叫到': bool(original['is_at'] or original['is_mentioned']), '无任务关键词仍回复': True}
        if '--live' in sys.argv:
            persona = json.loads(Path('/mnt/d/code/web/QQbot/persona-elaina.json').read_text())
            prompt = [{'role': 'system', 'content': persona['personality'] + '\n' + reply['extra_prompt']},
                      {'role': 'user', 'content': reply['reply_tool_args']['reply_reference']}]
            generated = await llm_service.generate(llm_service.LLMServiceRequest(task_name='replyer', request_type='plugin.direct_mention_probe', prompt=prompt, max_tokens=150))
            assert generated.success
            result['实际模型回放'] = generated.completion.response
        return result
    assert routed['output_items'][0]['tool_call']['func_name'] == 'analyze_task_image'
    deserialize_prompt_items(routed['output_items'], item_schema_version=CONTEXT_ITEM_SCHEMA_VERSION, mode=ContextProtocolMode.MODEL_OUTPUT)
    evidence = await plugin.flow.analyze(ctx, session, '96866415')
    assert evidence['success'], evidence
    assert seen_images
    routed = plugin.flow.after({'session_id': session, 'output_items': snapshot['output_items']})
    assert routed['output_items'][0]['tool_call']['func_name'] == 'reply'
    reply = plugin.flow.reply_before({'session_id': session, 'reply_message_id': '96866415', 'reply_tool_args': {}})
    reference = json.loads(reply['reply_tool_args']['reply_reference'].split('\n', 2)[-1])
    assert evidence['content'] == reference['image_analysis']['content']
    result = {'原消息': '96866415', '引用识图': True, '图片数量': len(seen_images), '等待改为执行任务': True, 'Context协议': '通过'}
    if '--live' in sys.argv:
        prompt = [{'role': 'system', 'content': reply['extra_prompt']},
                  {'role': 'user', 'content': reply['reply_tool_args']['reply_reference']}]
        generated = await llm_service.generate(llm_service.LLMServiceRequest(task_name='replyer', request_type='plugin.task_reply_probe', prompt=prompt, max_tokens=800))
        payload = generated.to_capability_payload()
        assert payload['success'], '任务回复生成失败'
        result['实际模型回放'] = payload.get('response') or payload.get('content')
    return result


with redirect_stdout(capture), redirect_stderr(capture):
    result = asyncio.run(replay())
print(json.dumps(result, ensure_ascii=False, indent=2))

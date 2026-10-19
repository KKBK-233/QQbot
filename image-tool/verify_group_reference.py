"""回放真实群图片及引用，检查导入和 DSF 工具选择，不生图、不发 QQ。"""
import argparse
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
from types import SimpleNamespace
import time

folder = Path(__file__).resolve().parent
spec = importlib.util.spec_from_file_location('group_reference_probe', folder / 'maibot-plugin/plugin.py', submodule_search_locations=[str(folder / 'maibot-plugin')])
module = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = module
spec.loader.exec_module(module)
from group_reference_probe.media_facts import available_media
from group_reference_probe.reply_facts import factual_context

async def verify(preflight, model=None, observe=False):
    """真实宿主只读；导入仅写独立测试编号，模型调用只检查返回的参数。"""
    os.chdir('/home/qqbot/QQbot/MaiBot')
    sys.path.insert(0, os.getcwd())
    from src.services import message_service, llm_service
    from src.plugin_runtime.host.message_utils import PluginMessageUtils
    from group_reference_probe.message_media import read_images
    session = 'eea9ec46d0880b86983de2e4235f4bca'
    async def get_message(mid, chat_id='', include_binary_data=False):
        msg = message_service.get_message_by_id(mid, chat_id=chat_id)
        return PluginMessageUtils._session_message_to_dict(msg, include_binary_data=include_binary_data) if msg else None
    ctx = SimpleNamespace(message=SimpleNamespace(get_by_id=get_message))
    items = [{'item_type': 'UserMessageItem', 'parts': [{'type': 'text', 'text': text}]} for text in [
        '<message msg_id="1485460619">\n[图片：黑色长发的魔女，戴着黑色尖帽和红色领带。]',
        '<message msg_id="1769802399" quote="1485460619">\n@KKBK 现在拿到了吧，生图']]
    facts = await available_media(ctx, {'session_id': session, 'items': items})
    assert len(facts) == 2 and all(f['source_message_ids'] == ['1485460619'] for f in facts)
    raw, _ = await read_images(ctx, session, '1769802399')
    plugin = module.create_plugin()
    plugin._ctx = ctx
    name = await plugin.import_reference(session, '1769802399', 'probe-group-original')
    path = Path('/mnt/d/code/web/QQbot/data/image-tool/inbox') / name
    try:
        assert hashlib.sha256(path.read_bytes()).digest() == hashlib.sha256(base64.b64decode(raw[0])).digest()
        image_size = path.stat().st_size
    finally:
        path.unlink()
    # 使用真实工具结构与合成上下文；不提交群聊历史，不执行返回的工具。
    snapshot = Path('logs/maisaka_prompt/planner/qq_group_1022382380/1791559202790.json')
    definitions = json.loads(snapshot.read_text())['tool_definitions']
    options = []
    for definition in definitions:
        function = definition.get('function', definition)
        if function['name'] in {'chatgpt_image', 'reply', 'analyze_task_image'}:
            options.append({'name': function['name'], 'description': function.get('description', ''),
                            'parameters_schema': function.get('parameters', function.get('parameters_schema'))})
    # 隔离对照不附带正在变化的群任务列表，保持原图事实、要求和工具输入固定。
    guidance = factual_context({'session_id': ''}, media=facts)['items'][-1]['parts'][0]['text']
    prompt = [{'role': 'system', 'content': guidance},
              {'role': 'assistant', 'content': '旧编号的原图没取到，请发原图。'},
              {'role': 'user', 'content': '用户后来已发图片，编号1485460619，引用它的要求编号1769802399：这张是《魔女之旅》芙兰。现在拿到了吧，生图，画她和伊蕾娜一起在庭院喝茶。请先执行需要的工具。'}]
    input_hash = hashlib.sha256(json.dumps({'prompt': prompt, 'tools': options},
                                          ensure_ascii=False, sort_keys=True).encode()).hexdigest()
    started = time.monotonic()
    result = await llm_service.generate(llm_service.LLMServiceRequest(
        task_name='planner', model_name=model or ('deepseek-v4-flash-thinking' if preflight else None),
        request_type='manual.group_original_regression', session_id='isolated-group-reference',
        prompt=prompt, tool_options=options, max_tokens=4096))
    assert result.success, '规划请求失败'
    completion = result.completion
    calls = completion.tool_calls or []
    accepted = [c for c in calls if c.func_name == 'chatgpt_image' and
                c.args.get('action') in {'generate', 'edit'} and
                str(c.args.get('message_id')) in {'1485460619', '1769802399'}]
    assert accepted or observe, '规划器没有直接使用用户引用原图：' + json.dumps([
        {'tool': c.func_name, 'action': c.args.get('action'), 'message_id': c.args.get('message_id')}
        for c in calls], ensure_ascii=False)
    trace = completion.generation_trace
    expected = 'OpenCodeGo' if model and model.startswith('opencode-go-') else 'DeepSeek'
    assert trace.provider == expected, '实际供应商与待测模型不一致'
    return {'original_bytes': image_size, 'quoted_original_hash_verified': True, 'input_hash': input_hash,
            'media_facts': facts, 'provider': trace.provider, 'model': trace.model,
            'seconds': round(time.monotonic() - started, 2), 'selected_action': accepted[0].args['action'] if accepted else None,
            'selected_message_id': accepted[0].args['message_id'] if accepted else None,
            'original_selected': bool(accepted), 'calls': [{'tool': c.func_name, 'args': c.args} for c in calls],
            'generation_requests': 0, 'qq_messages_sent': 0}

if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('--preflight', action='store_true')
    parser.add_argument('--model', choices=['deepseek-v4-flash-thinking', 'opencode-go-haiku-5.5-medium'])
    parser.add_argument('--observe', action='store_true', help='记录工具选择，允许先识图或回复，用于固定输入的模型对照')
    args = parser.parse_args()
    try:
        with redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()):
            report = asyncio.run(verify(args.preflight, args.model, args.observe))
    except Exception as exc:
        print(json.dumps({'success': False, 'error_type': type(exc).__name__, 'check': str(exc) if isinstance(exc, AssertionError) else 'model_or_media_failure'}))
        raise SystemExit(1)
    print(json.dumps(report, ensure_ascii=False))

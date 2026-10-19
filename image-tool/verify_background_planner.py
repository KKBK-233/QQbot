"""回放透明边缘任务和未指定背景的新画；只调文本模型，不执行生图或发送。"""
import asyncio
from contextlib import redirect_stdout, redirect_stderr
from copy import deepcopy
import importlib.util
import io
import json
import os
from pathlib import Path
import sys
import tempfile
import time

ROOT = Path('/home/qqbot/QQbot/MaiBot')
SOURCE = Path(__file__).resolve().parent
os.chdir(ROOT)
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(SOURCE))
from bridge import background_mode
spec = importlib.util.spec_from_file_location('background_probe_facts', SOURCE / 'maibot-plugin/reply_facts.py')
facts = importlib.util.module_from_spec(spec)
spec.loader.exec_module(facts)


async def verify():
    """新流程替换旧背景规则，保留实际改图上下文，另测无背景要求的文生图。"""
    from src.services import llm_service
    from src.plugin_runtime.hook_payloads import deserialize_prompt_items
    from src.llm_models.payload_content.context_item import CONTEXT_ITEM_SCHEMA_VERSION
    from src.llm_models.payload_content.context_protocol import ContextProtocolMode

    snapshot = ROOT / 'logs/maisaka_prompt/planner/qq_group_1022382380/1791297260674.json'
    original = json.loads(snapshot.read_text())
    tools = deepcopy(original['tool_definitions'])
    image_tool = next(t for t in tools if t.get('function', {}).get('name') == 'chatgpt_image')
    image_tool['function']['parameters']['properties']['prompt']['description'] += facts.BACKGROUND_GUIDANCE
    raw = [deepcopy(item) for item in original['request_items']
           if not any(p.get('text', '').startswith(facts.IDENTITY) for p in item.get('parts', []))]
    fresh = deepcopy(next(item for item in raw if any('以这个为原型做个DeepSeek娘的' in p.get('text', '')
                                                    for p in item.get('parts', []))))
    fresh['parts'] = [{'type': 'text', 'text': '<message msg_id="background-probe" time="22:34:15" user="群友">\n@KKBK 画一张DS娘数钱的表情包'}]
    cases = [('实际改图请求', raw), ('未指定背景的新画', [deepcopy(raw[0]), fresh])]
    results = []
    with tempfile.TemporaryDirectory() as empty_jobs:
        for name, context in cases:
            adjusted = facts.factual_context({'session_id': 'eea9ec46d0880b86983de2e4235f4bca', 'items': context}, root=Path(empty_jobs))
            items = deserialize_prompt_items(adjusted['items'], item_schema_version=CONTEXT_ITEM_SCHEMA_VERSION,
                                             mode=ContextProtocolMode.REQUEST_CONTEXT)
            if '--live' not in sys.argv:
                results.append({'case': name, 'protocol_ok': True})
                continue
            async def factory(*_args, **_kwargs):
                return items
            began = time.monotonic()
            generated = await llm_service.generate(llm_service.LLMServiceRequest(task_name='planner',
                request_type='plugin.background_probe', context_factory=factory, tool_options=tools, max_tokens=4000))
            assert generated.success, '规划回放失败'
            calls = [c for c in generated.completion.tool_calls or [] if c.func_name == 'chatgpt_image']
            assert len(calls) == 1, '没有直接生成唯一绘图请求'
            args = calls[0].args
            assert args.get('character') == 'deepseek', '角色身份丢失'
            prompt = args.get('prompt', '')
            assert background_mode(prompt, args.get('action') == 'edit') == 'opaque', '未采用不透明输出'
            if name == '实际改图请求':
                assert args.get('action') == 'edit', '原图被改成重新生成'
                assert str(args.get('message_id')) in {'1953377817', '-101611979'}, '编辑对象丢失'
                assert '保留' in prompt and '背景' in prompt, '已有背景未保留'
            else:
                assert args.get('action') == 'generate' and not args.get('message_id'), '新画错误继承原图'
                assert any(scene in prompt for scene in ('房间', '客厅', '书房', '桌', '柜台', '银行', '金库', '办公室', '室内', '店铺', '街', '窗')), '没有选择具体场景'
                assert not any(word in prompt for word in ('背景随意', '简洁背景', '纯色背景', '白底')), '背景仍被默认简化'
            results.append({'case': name, 'seconds': round(time.monotonic() - began, 2),
                            'requested_background': 'opaque', 'args': args})
    return {'cases': results, 'tools_executed': 0, 'qq_messages_sent': 0}


if __name__ == '__main__':
    try:
        with redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()):
            result = asyncio.run(verify())
        print(json.dumps(result, ensure_ascii=False, indent=2))
    except Exception as error:
        print(json.dumps({'success': False, 'error_type': type(error).__name__, 'detail': str(error)[:1200]}, ensure_ascii=False))
        raise SystemExit(1)

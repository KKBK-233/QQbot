"""回放新画和局部改图请求，只调用规划模型，不执行生图或发送QQ。"""
import ast
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
spec = importlib.util.spec_from_file_location('prompt_detail_facts', SOURCE / 'maibot-plugin/reply_facts.py')
facts = importlib.util.module_from_spec(spec)
spec.loader.exec_module(facts)


def prompt_description():
    """从当前工具声明读取真实参数说明，避免沿用历史快照中的旧规则。"""
    tree = ast.parse((SOURCE / 'maibot-plugin/plugin.py').read_text(encoding='utf-8'))
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call) or not isinstance(node.func, ast.Name) or node.func.id != 'ToolParameterInfo':
            continue
        keywords = {value.arg: value.value for value in node.keywords}
        if ast.literal_eval(keywords['name']) == 'prompt':
            return eval(compile(ast.Expression(keywords['description']), '<tool-description>', 'eval'),
                        {'__builtins__': {}, 'IMAGE_PROMPT_GUIDANCE': facts.IMAGE_PROMPT_GUIDANCE,
                         'BACKGROUND_GUIDANCE': facts.BACKGROUND_GUIDANCE})
    raise AssertionError('没有找到当前prompt参数说明')


async def verify():
    """同一模型验证视觉细化与编辑边界，返回实际生成的工具参数供检查。"""
    from src.services import llm_service
    from src.plugin_runtime.hook_payloads import deserialize_prompt_items
    from src.llm_models.payload_content.context_item import CONTEXT_ITEM_SCHEMA_VERSION
    from src.llm_models.payload_content.context_protocol import ContextProtocolMode

    original = json.loads((ROOT / 'logs/maisaka_prompt/planner/qq_group_1022382380/1791385625247.json').read_text())
    tools = deepcopy(original['tool_definitions'])
    image_tool = next(t for t in tools if t.get('function', {}).get('name') == 'chatgpt_image')
    image_tool['function']['parameters']['properties']['prompt']['description'] = prompt_description()
    fresh = deepcopy(next(item for item in original['request_items']
                          if any('我要看你穿白丝的样子' in p.get('text', '') for p in item.get('parts', []))))
    cases = [('新画白丝伊蕾娜', '<message msg_id="1703245752" time="22:47:05" user="群友">\n@伊蕾娜 我要看你穿白丝的样子（'),
             ('只改袜子颜色', '<message msg_id="prompt-edit-probe" time="22:47:06" user="群友">\n@伊蕾娜 修改我引用的这张图（唯一原图message_id=-306359998）：只将白色过膝袜改成黑色，其余保持不变。')]
    results = []
    with tempfile.TemporaryDirectory() as empty_jobs:
        for name, text in cases:
            message = deepcopy(fresh)
            message['parts'] = [{'type': 'text', 'text': text}]
            adjusted = facts.factual_context({'session_id': 'eea9ec46d0880b86983de2e4235f4bca',
                                              'items': [deepcopy(original['request_items'][0]), message]},
                                             root=Path(empty_jobs))
            items = deserialize_prompt_items(adjusted['items'], item_schema_version=CONTEXT_ITEM_SCHEMA_VERSION,
                                             mode=ContextProtocolMode.REQUEST_CONTEXT)
            if '--live' not in sys.argv and '--recorded' not in sys.argv:
                results.append({'case': name, 'protocol_ok': True})
                continue
            async def factory(*args, **kwargs):
                return items
            result_path = SOURCE.parent / 'data' / ('prompt-detail-generate.json' if name == '新画白丝伊蕾娜' else 'prompt-detail-edit.json')
            started = time.monotonic()
            if '--recorded' in sys.argv:
                # 修正断言后复核已保存的真实结果，不重复消耗文本模型额度。
                args = json.loads(result_path.read_text(encoding='utf-8'))
            else:
                generated = await llm_service.generate(llm_service.LLMServiceRequest(task_name='planner',
                    request_type='plugin.prompt_detail_probe', context_factory=factory, tool_options=tools, max_tokens=4000))
                assert generated.success, '规划回放失败'
                calls = [call for call in generated.completion.tool_calls or [] if call.func_name == 'chatgpt_image']
                assert len(calls) == 1, '没有提交唯一绘图请求'
                args = calls[0].args
                result_path.write_text(json.dumps(args, ensure_ascii=False, indent=2), encoding='utf-8')
            prompt = args.get('prompt', '')
            if name == '新画白丝伊蕾娜':
                assert args.get('action') == 'generate' and not args.get('message_id')
                assert all(word in prompt for word in ('伊蕾娜', '银灰', '紫', '白'))
                assert len(prompt) >= 250, '新画仍未展开视觉描述'
            else:
                assert args.get('action') == 'edit' and str(args.get('message_id')) == '-306359998'
                assert '黑' in prompt and any(word in prompt for word in ('保留', '保持不变', '沿用')) and len(prompt) < 300, '局部编辑越界扩写'
            results.append({'case': name, 'seconds': round(time.monotonic() - started, 2),
                            'prompt_characters': len(prompt), 'args': args})
    return {'cases': results, 'recorded_results': '--recorded' in sys.argv, 'tools_executed': 0, 'qq_messages_sent': 0}


if __name__ == '__main__':
    try:
        with redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()):
            result = asyncio.run(verify())
        print(json.dumps(result, ensure_ascii=False, indent=2))
    except Exception as error:
        print(json.dumps({'success': False, 'error_type': type(error).__name__, 'detail': str(error)[:800]}, ensure_ascii=False))
        raise SystemExit(1)

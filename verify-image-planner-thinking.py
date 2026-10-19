"""真实对话回放与连续工具协议检查；只调文本模型，不执行工具或发群消息。"""
import asyncio
import copy
from contextlib import redirect_stdout, redirect_stderr
import importlib.util
import io
import json
import os
from pathlib import Path
import sys
import tempfile
import time
from types import SimpleNamespace

ROOT = Path('/home/qqbot/QQbot/MaiBot')
os.chdir(ROOT)
sys.path[:0] = [str(ROOT), '/mnt/d/code/web/QQbot']
spec = importlib.util.spec_from_file_location('image_probe_facts', Path('/mnt/d/code/web/QQbot/image-tool/maibot-plugin/reply_facts.py'))
facts = importlib.util.module_from_spec(spec)
spec.loader.exec_module(facts)


async def verify():
    """先验证不猜陌生称呼，再用真人纠正验证原图与注册角色同时传递。"""
    from src.services import llm_service
    from src.plugin_runtime.hook_payloads import deserialize_prompt_items
    from src.llm_models.payload_content.context_item import CONTEXT_ITEM_SCHEMA_VERSION, ContextItemMeta, FunctionCallOutputItem
    from src.llm_models.payload_content.context_protocol import ContextProtocolMode
    from src.llm_models.model_client.openai_client import _convert_messages
    from deepseek_thinking_compat import convert_thinking_messages
    from src.services import message_service
    from src.plugin_runtime.host.message_utils import PluginMessageUtils
    media_spec = importlib.util.spec_from_file_location('planner_probe_media', spec.origin.replace('reply_facts.py', 'message_media.py'))
    media = importlib.util.module_from_spec(media_spec)
    media_spec.loader.exec_module(media)
    folder = ROOT / 'logs/maisaka_prompt/planner/qq_group_1022382380'
    original = json.loads((folder / '1791201887999.json').read_text())
    session = 'eea9ec46d0880b86983de2e4235f4bca'
    async def get_message(mid, chat_id='', include_binary_data=False):
        message = message_service.get_message_by_id(mid, chat_id=chat_id)
        return PluginMessageUtils._session_message_to_dict(message, include_binary_data=include_binary_data) if message else None
    originals, sources = await media.read_images(SimpleNamespace(message=SimpleNamespace(get_by_id=get_message)), session, '25358973')
    assert len(originals) == 1, '引用原图不可读或有多图歧义'
    results = []
    with tempfile.TemporaryDirectory() as empty_jobs:
        for corrected in ((True,) if '--corrected-only' in sys.argv else (False, True)):
            raw = copy.deepcopy(original['request_items'])
            if corrected:
                # 用真人纠正澄清原请求，验证已知主体的改图；不把后来问话当成重试授权。
                later = json.loads((folder / '1791202007108.json').read_text())
                assert any('你知道大肥鱼是指ds娘的对吧' in part.get('text', '') for value in later['request_items'] for part in value.get('parts', []))
                for value in raw:
                    for part in value.get('parts', []):
                        if 'text' in part:
                            part['text'] = part['text'].replace('换成大肥鱼，不过得是人偶', '换成大肥鱼（指ds娘），不过得是人偶')
            adjusted = facts.factual_context({'session_id': session, 'items': raw}, root=Path(empty_jobs))
            items = deserialize_prompt_items(adjusted['items'], item_schema_version=CONTEXT_ITEM_SCHEMA_VERSION, mode=ContextProtocolMode.REQUEST_CONTEXT)
            async def factory(*args, **kwargs):
                return items
            started = time.monotonic()
            generated = await llm_service.generate(llm_service.LLMServiceRequest(task_name='planner', request_type='plugin.image_thinking_probe',
                context_factory=factory, tool_options=original['tool_definitions'], max_tokens=4000))
            assert generated.success, '规划回放失败'
            calls = [{'tool': c.func_name, 'args': c.args} for c in generated.completion.tool_calls or []]
            reasoning = generated.completion.reasoning
            assert reasoning, '实际请求未返回原生推理'
            if corrected:
                images = [c['args'] for c in calls if c['tool'] == 'chatgpt_image']
                assert images and images[0].get('action') == 'edit', '纠正后仍未编辑原图：' + json.dumps(calls, ensure_ascii=False)
                assert str(images[0].get('message_id')) in {'25358973', '1401443085'}, '未指定引用原图'
                assert images[0].get('character') == 'deepseek', '未选择DS娘角色卡'
                assert not any(word in images[0].get('prompt', '') for word in ('魔女', '银灰色长发')), '自身外观混入其他角色'
            else:
                assert not any(c['tool'] == 'chatgpt_image' for c in calls), '未经确认猜测了称呼'
                assert any(c['tool'] in {'query_memory', 'reply'} for c in calls), '未查记忆或确认称呼'
            results.append({'case': '按真人纠正重述的改图请求' if corrected else '未知称呼', 'seconds': round(time.monotonic()-started, 2),
                            'native_reasoning_present': True, 'calls': calls})
            # 同一真实客户端回传未执行工具的测试占位结果，验证第二轮不会丢协议字段。
            if not corrected:
                history = [*items, *generated.completion.output_items]
                for call in generated.completion.tool_calls or []:
                    turn = next(value.meta.logical_turn_id for value in generated.completion.output_items
                                if getattr(getattr(value, 'tool_call', None), 'call_id', None) == call.call_id)
                    history.append(FunctionCallOutputItem(meta=ContextItemMeta.create(logical_turn_id=turn), call_id=call.call_id,
                        tool_name=call.func_name, output='【离线回放占位结果】该工具没有实际执行；没有找到称呼对应的角色，请确认含义。'))
                projected = convert_thinking_messages(history, _convert_messages)
                assert any(message.get('reasoning_content') for message in projected if message.get('role') == 'assistant'), '推理未绑定对应助手消息'
                async def next_factory(*args, **kwargs):
                    return history
                followup = await llm_service.generate(llm_service.LLMServiceRequest(task_name='planner', request_type='plugin.thinking_protocol_probe',
                    context_factory=next_factory, tool_options=original['tool_definitions'], max_tokens=2000))
                assert followup.success, '连续工具思考请求未通过'
                results[-1]['second_round_protocol_ok'] = True
    return {'cases': results, 'original_readable': True, 'source_messages': sources, 'tools_executed': 0, 'qq_messages_sent': 0}


if __name__ == '__main__':
    capture = io.StringIO()
    try:
        with redirect_stdout(capture), redirect_stderr(capture):
            result = asyncio.run(verify())
        print(json.dumps(result, ensure_ascii=False, indent=2))
    except Exception as error:
        print(json.dumps({'success': False, 'error_type': type(error).__name__, 'detail': str(error)[:1500]}, ensure_ascii=False))
        raise SystemExit(1)

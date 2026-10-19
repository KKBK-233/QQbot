"""回放原群友印象请求，验证真实规划器先查完整人物目录，不发QQ。"""
import asyncio
from contextlib import redirect_stdout, redirect_stderr
from copy import deepcopy
import io
import json
import os
from pathlib import Path
import sys
import time
from types import SimpleNamespace

ROOT = Path('/home/qqbot/QQbot/MaiBot')


async def verify():
    """仅执行只读目录查询，其它工具不执行；验证目录反馈后的下一步规划。"""
    from src.services import llm_service
    from src.services.person_memory_index import directory_reference, get_runtime_index
    from src.maisaka.builtin_tool.list_known_people import get_tool_spec, format_directory
    from src.plugin_runtime.hook_payloads import deserialize_prompt_items
    from src.llm_models.payload_content.context_item import CONTEXT_ITEM_SCHEMA_VERSION, ContextItemMeta, FunctionCallOutputItem
    from src.llm_models.payload_content.context_protocol import ContextProtocolMode
    original = json.loads((ROOT / 'logs/maisaka_prompt/planner/qq_group_1022382380/1791347510062.json').read_text())
    raw = deepcopy(original['request_items'])
    anchor = SimpleNamespace(platform='qq', message_info=SimpleNamespace(group_info=SimpleNamespace(group_id='1022382380')))
    reference = await directory_reference(anchor)
    extra = deepcopy(next(item for item in raw if item['item_type'] == 'SystemMessageItem'))
    extra['meta']['item_id'] = ContextItemMeta.create().item_id
    extra['parts'] = [{'type': 'text', 'text': reference}]
    raw.append(extra)
    items = deserialize_prompt_items(raw, item_schema_version=CONTEXT_ITEM_SCHEMA_VERSION, mode=ContextProtocolMode.REQUEST_CONTEXT)
    tools = deepcopy(original['tool_definitions']) + [get_tool_spec().to_llm_definition()]
    async def factory(*args, **kwargs):
        return items
    began = time.monotonic()
    generated = await llm_service.generate(llm_service.LLMServiceRequest(task_name='planner',
        request_type='plugin.person_index_probe', context_factory=factory, tool_options=tools, max_tokens=3500))
    assert generated.success, '索引规划回放请求失败'
    calls = generated.completion.tool_calls or []
    assert any(call.func_name == 'list_known_people' for call in calls), '没有先查询完整人物目录'
    directory = get_runtime_index().list_people(platform='qq', group_id='1022382380')
    history = [*items, *generated.completion.output_items]
    for call in calls:
        turn = next(item.meta.logical_turn_id for item in generated.completion.output_items
            if getattr(getattr(item, 'tool_call', None), 'call_id', None) == call.call_id)
        output = format_directory(directory) if call.func_name == 'list_known_people' else '【隔离回放】该工具未执行，请依据完整目录及已返回的历史消息继续规划。'
        history.append(FunctionCallOutputItem(meta=ContextItemMeta.create(logical_turn_id=turn),
            call_id=call.call_id, tool_name=call.func_name, output=output))
    async def followup_factory(*args, **kwargs):
        return history
    followup = await llm_service.generate(llm_service.LLMServiceRequest(task_name='planner',
        request_type='plugin.person_index_followup_probe', context_factory=followup_factory, tool_options=tools, max_tokens=3500))
    assert followup.success, '目录返回后的规划请求失败'
    next_calls = [{'tool': c.func_name, 'args': c.args} for c in followup.completion.tool_calls or []]
    other_people = [p for p in directory['people'] if p['name'] not in ('qwert', 'dzu')]
    visible = json.dumps(next_calls, ensure_ascii=False)
    covered = [p['name'] for p in other_people if p['name'] in visible or p['person_id'] in visible]
    assert covered, '目录已返回，但下一步仍只关注原来的两人'
    return {'success': True, 'historical_group_people_returned': directory['total'],
        'first_round_tools': [c.func_name for c in calls], 'next_round_tools': next_calls,
        'additional_people_considered': covered, 'seconds': round(time.monotonic()-began, 2),
        'qq_messages_sent': 0, 'image_generation_calls': 0, 'profile_writes': 0}


if __name__ == '__main__':
    os.chdir(ROOT)
    sys.path.insert(0, str(ROOT))
    try:
        with redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()):
            result = asyncio.run(verify())
        print(json.dumps(result, ensure_ascii=False, indent=2))
    except Exception as error:
        print(json.dumps({'success': False, 'error_type': type(error).__name__, 'detail': str(error)[:500]}, ensure_ascii=False))
        raise SystemExit(1)

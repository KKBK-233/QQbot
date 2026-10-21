"""在生产虚拟环境检查线上引擎；--stage 才检查暂存副本，默认模拟模型和发送。"""

from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch
import asyncio
import json
import os
import sys

ROOT = Path('/home/qqbot/QQbot/MaiBot')
STAGE = Path('/home/qqbot/QQbot/kkbk-engine-stage')
os.chdir(ROOT)
sys.path.insert(0, str(ROOT))
import src
if '--stage' in sys.argv:
    src.__path__.insert(0, str(STAGE / 'src'))

from src.common.data_models.llm_service_data_models import LLMResponseResult
from src.core.tooling import ToolExecutionResult, ToolSpec
from src.kkbk_engine.actions import ModelAction
from src.kkbk_engine.hooks import register_kkbk_hook_specs
from src.kkbk_engine.inbound import InboundMessage
from src.kkbk_engine.runtime import KKBKHeartFlowChatting, text_item
from src.llm_models.payload_content.context_item import FunctionCallOutputItem, RoleType
from src.llm_models.payload_content.tool_option import ToolCall
from src.plugin_runtime.host.hook_spec_registry import HookSpecRegistry


def response(action='reply', text='收到', task='', calls=None):
    """构造真正的宿主原生 Items，覆盖思考和工具结果的协议拼接。"""
    content = '' if calls else json.dumps({'action': action, 'text': text, 'task': task, 'track': 'leave'})
    return LLMResponseResult.from_portable_output(response=content, reasoning='模拟推理', tool_calls=calls)


def runtime():
    """只替换聊天流目录，不替换模型配置和工具目录实现。"""
    stream = SimpleNamespace(platform='qq', group_id='1022382380', user_id='', is_group_session=True)
    with patch('src.kkbk_engine.runtime.chat_manager.get_session_by_session_id', return_value=stream), \
         patch('src.kkbk_engine.runtime.chat_manager.get_session_name', return_value='离线验收'):
        value = KKBKHeartFlowChatting('kkbk-offline-test')
    return value


def open_look(value):
    value.gate.observe(InboundMessage(message_id='anchor', timestamp=1, user_id='1020050112',
                        nickname='操作员', text='KKBK在吗', mentions=('3976405558',)))
    return value.gate.step(1)


async def main():
    """验证普通单次回复、失败补试、工具后失败不重放、宿主身份绑定及公开接口。"""
    print('验证引擎来源：', sys.modules[KKBKHeartFlowChatting.__module__].__file__)
    value = runtime()
    specs = await value._registry.list_tools(value._availability(None))
    names = {spec.name for spec in specs}
    assert {'remember_memory', 'query_memory', 'list_known_people', 'query_person_profile', 'wait'} <= names, names
    registry = HookSpecRegistry()
    assert len(register_kkbk_hook_specs(registry)) == 2
    value._prepare = AsyncMock(return_value=[text_item(RoleType.User, '离线请求')])
    value._send = AsyncMock()
    value._after_turn = AsyncMock()
    value._llm.generate_response_with_context = AsyncMock(return_value=response())
    await value._turn(open_look(value))
    assert value._llm.generate_response_with_context.await_count == 1
    assert value._send.await_count == 1
    assert value.gate.in_flight is None

    value = runtime()
    value._prepare = AsyncMock(return_value=[text_item(RoleType.User, '离线请求')])
    value._send = AsyncMock()
    value._after_turn = AsyncMock()
    value._llm.generate_response_with_context = AsyncMock(side_effect=[OSError('模拟网络断开'), response()])
    await value._turn(open_look(value))
    assert value._llm.generate_response_with_context.await_count == 2
    assert value._send.await_count == 1

    value = runtime()
    value._prepare = AsyncMock(return_value=[text_item(RoleType.User, '离线请求')])
    value._send = AsyncMock()
    value._after_turn = AsyncMock()
    value._llm.generate_response_with_context = AsyncMock(return_value=response('escalate', '', '模拟搜索任务'))
    call = ToolCall(call_id='call_once', func_name='web_search', args={'query': '离线验收'})
    seen = []

    async def heavy(factory, options):
        seen.append(awaitable_items(factory))
        if len(seen) == 1:
            return response(calls=[call])
        if len(seen) == 2:
            raise OSError('工具已完成，回答时网络断开')
        return response(text='模拟搜索完成')

    value._tool_llm.generate_response_with_context = heavy
    value._invoke = AsyncMock(return_value=ToolExecutionResult(tool_name='web_search', success=True, content='模拟来源'))
    await value._turn(open_look(value))
    assert value._invoke.await_count == 1
    assert value._send.await_count == 1
    assert any(isinstance(item, FunctionCallOutputItem) and item.output == '模拟来源' for item in seen[-1])

    value = runtime()
    value._registry.invoke = AsyncMock(return_value=ToolExecutionResult(tool_name='web_search', success=True, content='模拟来源'))
    anchor = SimpleNamespace(message_info=SimpleNamespace(user_info=SimpleNamespace(user_id='1020050112')))
    forged = ToolCall(call_id='identity', func_name='web_search', args={
        'query': '离线验收', 'user_id': 'forged', 'stream_id': 'other', '_sandbox_trusted_context': True})
    await value._invoke(forged, [ToolSpec(name='web_search')], anchor, wait_budget=[240])
    invocation, context = value._registry.invoke.await_args.args
    assert context.user_id == '1020050112' and context.session_id == 'kkbk-offline-test'
    assert invocation.arguments == {'query': '离线验收'}
    from src.llm_models.exceptions import RespNotOkException
    assert KKBKHeartFlowChatting._retryable(RespNotOkException(401)) is False
    assert value.config.gate.debounce_seconds == 3 and value.config.history.max_entries == 80
    # 使用真实会话记录检查入站映射、历史恢复和请求组装，不触发发送。
    from src.common.message_repository import find_messages
    from src.kkbk_engine.runtime import from_session_message
    recent = await asyncio.to_thread(find_messages, group_id='1022382380', limit=2)
    assert recent
    incoming = from_session_message(recent[-1])
    assert incoming.message_id and incoming.timestamp > 0
    value._append(recent[-1], observe=False)
    manager = SimpleNamespace(invoke_hook=AsyncMock(side_effect=lambda name, **kwargs: SimpleNamespace(kwargs=kwargs)))
    with patch('src.kkbk_engine.request_context.get_plugin_runtime_manager', return_value=manager):
        prepared = await value._prepare(open_look(value), specs)
    assert len(prepared) == 2
    print('真实历史与配置组装通过')
    if '--live' in sys.argv:
        await live_probe()
    print('验收通过：单次普通回复、一次补试、工具后续重试不重放、宿主身份、记忆工具、3秒/80条')


def awaitable_items(factory):
    """运行时工厂无需客户端即可构建已有原生 Items。"""
    return list(factory(None))


async def live_probe():
    """只调用现有模型验证 JSON 和原生工具往返；工具为本地模拟，不执行外部动作。"""
    from src.common.data_models.llm_service_data_models import LLMGenerationOptions
    from src.kkbk_engine.actions import parse_action
    from src.llm_models.payload_content.context_item import ContextItemMeta, FunctionCallItem
    from src.llm_models.payload_content.resp_format import RespFormat, RespFormatType
    from src.services.llm_service import LLMServiceClient
    client = LLMServiceClient('planner', request_type='kkbk.acceptance')
    items = [text_item(RoleType.System, '这是无外部副作用的连通测试。最终只输出JSON：'
                       '{"action":"reply","text":"连通正常","track":"leave"}。'),
             text_item(RoleType.User, '请给出连通正常的JSON。')]
    options = LLMGenerationOptions(max_tokens=1024, response_format=RespFormat(format_type=RespFormatType.JSON_OBJ))
    first = await client.generate_response_with_context(lambda client, model_info=None: list(items), options)
    assert parse_action(first.response).action == 'reply', first.response
    print('真实模型轻路径通过:', first.model_name)
    items.extend(first.output_items)
    items.append(text_item(RoleType.User, '现在先调用kkbk_probe，然后根据工具结果输出同样的最终JSON。'))
    spec = ToolSpec(name='kkbk_probe', description='无副作用的本地连通测试', parameters_schema={
        'type': 'object', 'properties': {}, 'required': []})
    options.tool_options = [spec.to_llm_definition()]
    second = await client.generate_response_with_context(lambda client, model_info=None: list(items), options)
    assert second.tool_calls and second.tool_calls[0].func_name == 'kkbk_probe', second.response
    items.extend(second.output_items)
    for call in second.tool_calls:
        call_item = next(item for item in second.output_items if isinstance(item, FunctionCallItem)
                         and item.tool_call.call_id == call.call_id)
        items.append(FunctionCallOutputItem(meta=ContextItemMeta.create(logical_turn_id=call_item.meta.logical_turn_id),
            call_id=call.call_id, tool_name=call.func_name, output='{"success":true,"content":"连通正常"}'))
    final = await client.generate_response_with_context(lambda client, model_info=None: list(items), options)
    assert not final.tool_calls and parse_action(final.response).action == 'reply', final.response
    print('真实模型原生工具往返通过:', final.model_name)


if __name__ == '__main__':
    asyncio.run(main())

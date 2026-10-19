"""穿过MaiBot真实可选客户端验证代码工具及续接，不发QQ、不执行工具。"""
import asyncio
import argparse
from contextlib import redirect_stderr, redirect_stdout
import io
import json
import os
from pathlib import Path
import sys
import time
import traceback

ROOT = Path('/home/qqbot/QQbot/MaiBot')
MODEL_NAME = 'opencode-go-haiku-5.5'


async def verify(model_name=MODEL_NAME):
    """仅发送合成编码任务，两次请求沿用同一稳定会话编号。"""
    os.chdir(ROOT)
    sys.path.insert(0, str(ROOT))
    from src.config.config import config_manager
    from src.llm_models.model_client.base_client import ResponseRequest, RequestTraceContext, client_registry
    from src.llm_models.model_client import ensure_client_type_loaded
    from src.llm_models.payload_content.context_item import ContextItemBuilder, ContextItemMeta, RoleType
    from src.llm_models.payload_content.tool_option import ToolOption
    config = config_manager.get_model_config()
    model = next(m for m in config.models if m.name == model_name)
    provider = next(p for p in config.api_providers if p.name == model.api_provider)
    ensure_client_type_loaded(provider.client_type)
    client = client_registry.get_client_class_instance(provider)
    original_tasks = {name: task.model_list[:] for name, task in vars(config.model_task_config).items() if hasattr(task, 'model_list')}
    # 工具仅用于验证协议，返回结果由测试构造，不执行模型请求的命令。
    tool = ToolOption(name='probe_tests', description='合成Python测试探针，不执行真实代码。',
        parameters_schema_override={'type': 'object', 'properties': {'module': {'type': 'string'}}, 'required': ['module']})
    items = [ContextItemBuilder().set_role(RoleType.System).add_text_content(
        '这是隔离的编码工具兼容测试。必须先调用probe_tests且module为first.py；拿到模拟结果后用一句中文说明，不再次调用工具。').build(),
        ContextItemBuilder().set_role(RoleType.User).add_text_content(
        '代码def first(xs): return xs[0]在空列表失败。建议修复为return xs[0] if xs else None。请检查first.py的模拟测试结果。').build()]
    trace = RequestTraceContext(task_name='opencode_go_probe', request_type='manual.coding_compatibility',
                                session_id='qqbot-opencode-haiku55-coding-probe')
    request = ResponseRequest(model_info=model, context_items=items, tool_options=[tool], max_tokens=2048, trace_context=trace)
    start = time.monotonic()
    first = await client.get_response(request)
    first_seconds = round(time.monotonic() - start, 2)
    calls = first.tool_calls or []
    if len(calls) != 1 or calls[0].func_name != 'probe_tests' or calls[0].args != {'module': 'first.py'}:
        raise RuntimeError('模型未按预期返回代码测试工具')
    continuation = items + list(first.output_items) + [ContextItemBuilder().set_role(RoleType.Tool).set_meta(ContextItemMeta.create(logical_turn_id=request.logical_turn_id))
        .set_tool_call_id(calls[0].call_id).add_text_content(json.dumps({'simulated': True, 'passed': True, 'tests': ['empty_list', 'nonempty_list']})).build()]
    start = time.monotonic()
    second = await client.get_response(request.copy_with(context_items=continuation))
    second_seconds = round(time.monotonic() - start, 2)
    if not second.content or second.tool_calls:
        raise RuntimeError('工具结果续接未得到完整文本')
    effort = next((level for level in ('low', 'medium') if model_name.endswith('-' + level)), None)
    if effort:
        for response in [first, second]:
            if response.request_wire_payload.get('thinking') != {'type': 'adaptive', 'display': 'omitted'} or response.request_wire_payload.get('output_config') != {'effort': effort}:
                raise RuntimeError('实际请求未使用adaptive/' + effort)
        replayed = second.request_wire_payload['messages'][-2]['content']
        if replayed != first.provider_response['content']:
            raise RuntimeError('原生工具续接未完整回放响应')
    if original_tasks != {name: task.model_list[:] for name, task in vars(config.model_task_config).items() if hasattr(task, 'model_list')}:
        raise RuntimeError('生产任务被意外修改')
    report = {'success': True, 'provider': provider.name, 'model': second.generation_trace.model,
        'model_alias': model_name, 'thinking': first.request_wire_payload.get('thinking'),
        'output_config': first.request_wire_payload.get('output_config'),
        'native_replay_verified': effort is not None,
        'wire_protocol': second.wire_protocol, 'tool': calls[0].func_name, 'tool_seconds': first_seconds,
        'continuation_seconds': second_seconds, 'reply': second.content,
        'tokens': {'input': first.usage.prompt_tokens + second.usage.prompt_tokens,
                   'output': first.usage.completion_tokens + second.usage.completion_tokens},
        'tools_executed': 0, 'qq_messages_sent': 0, 'memory_writes': 0, 'production_tasks_changed': False}
    folder = ROOT / 'data/opencode-go'
    folder.mkdir(parents=True, exist_ok=True, mode=0o700)
    path = folder / 'verification.json'
    fd = os.open(path, os.O_CREAT | os.O_WRONLY | os.O_TRUNC, 0o600)
    with os.fdopen(fd, 'w') as handle:
        json.dump(report, handle, ensure_ascii=False, indent=2)
    path.chmod(0o600)
    return report


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('--model', default=MODEL_NAME)
    args = parser.parse_args()
    try:
        # 初始化日志不作为测试输出；异常只报告类型，不输出可能含敏感正文的堆栈。
        with redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()):
            result = asyncio.run(verify(args.model))
    except Exception as error:
        print(json.dumps({'success': False, 'error_type': type(error).__name__, 'response_meta': getattr(error, 'ext_info', None), 'frames': [{'file': Path(frame.filename).name, 'line': frame.lineno, 'function': frame.name} for frame in traceback.extract_tb(error.__traceback__)]}, ensure_ascii=False))
        raise SystemExit(1)
    print(json.dumps(result, ensure_ascii=False))

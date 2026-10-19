"""回放一个真实回复失败快照，只验证模型，不执行工具、不向 QQ 发送消息。"""
import asyncio
from contextlib import redirect_stdout, redirect_stderr
import io
import json
import os
from pathlib import Path
import sys

ROOT = Path('/home/qqbot/QQbot/MaiBot')


async def verify():
    """选取助手开场的回复快照，验证补起点后仍保留正文与最新用户任务。"""
    os.chdir(ROOT)
    sys.path.insert(0, str(ROOT))
    from src.config.config import config_manager
    from src.llm_models.model_client.base_client import ResponseRequest, RequestTraceContext, client_registry
    from src.llm_models.request_snapshot import deserialize_context_item_snapshot
    from src.llm_models.model_client.opencode_go_native import convert_context, native_scope

    config = config_manager.get_model_config()
    live = config.model_task_config.planner.model_list[0]
    model = next(m for m in config.models if m.name == live and m.name.startswith('opencode-go-haiku-5.5-'))
    provider = next(p for p in config.api_providers if p.name == model.api_provider)
    client = client_registry.get_client_class_instance(provider)
    files = sorted((ROOT / 'logs/maisaka_prompt/llm_error').rglob('*.json'),
                   key=lambda p: p.stat().st_mtime, reverse=True)
    for path in files:
        snapshot = json.loads(path.read_text())
        if snapshot['request'].get('request_type') != 'maisaka.replyer':
            continue
        items = [deserialize_context_item_snapshot(it) for it in snapshot['request_items']]
        messages = convert_context(items, native_scope(provider))
        dialog = [m for m in messages if m['role'] not in {'system', 'developer'}]
        if dialog and dialog[0]['role'] == 'assistant':
            break
    else:
        raise RuntimeError('没有找到助手开场的回复失败快照')
    response = await client.get_response(ResponseRequest(model_info=model, context_items=items,
        max_tokens=1024, trace_context=RequestTraceContext(session_id='isolated-history-window-probe',
        task_name='history_window_probe', request_type='manual.history_window_probe')))
    payload = response.request_wire_payload
    if not response.content or response.tool_calls:
        raise RuntimeError('真实失败快照没有获得完整文字回复')
    if payload['messages'][0]['role'] != 'user' or payload['messages'][1]['role'] != 'assistant':
        raise RuntimeError('历史起点或原助手角色丢失')
    report = {'success': True, 'snapshot_name': path.name, 'model': response.generation_trace.model,
              'history_started_with_assistant': True, 'original_history_items': len(items),
              'reply_characters': len(response.content), 'input_tokens': response.usage.prompt_tokens,
              'output_tokens': response.usage.completion_tokens, 'tools_executed': 0,
              'qq_messages_sent': 0, 'memory_writes': 0}
    target = ROOT / 'data/opencode-go/history-window-verification.json'
    fd = os.open(target, os.O_CREAT | os.O_WRONLY | os.O_TRUNC, 0o600)
    with os.fdopen(fd, 'w') as handle:
        json.dump(report, handle, ensure_ascii=False, indent=2)
    return report


if __name__ == '__main__':
    try:
        with redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()):
            report = asyncio.run(verify())
    except Exception as error:
        print(json.dumps({'success': False, 'error_type': type(error).__name__}))
        raise SystemExit(1)
    print(json.dumps(report, ensure_ascii=False))

"""回放真实群聊请求，核对引用目标解析，不执行任何返回工具。"""
import asyncio
from contextlib import redirect_stdout, redirect_stderr
import importlib.util
import io
import json
import os
from pathlib import Path
import sys
import time
from types import SimpleNamespace

folder = Path(__file__).resolve().parent / 'maibot-plugin'
spec = importlib.util.spec_from_file_location('explicit_quote_probe', folder / 'media_facts.py', submodule_search_locations=[str(folder)])
media = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = media
spec.loader.exec_module(media)
from explicit_quote_probe.reply_facts import factual_context

async def main():
    """回放公开对话和全部工具，快照不含签名，不作为原生续接协议验收。"""
    os.chdir('/home/qqbot/QQbot/MaiBot')
    sys.path.insert(0, os.getcwd())
    from src.services import message_service
    from src.plugin_runtime.host.message_utils import PluginMessageUtils
    from src.config.config import config_manager
    from src.llm_models.model_client.base_client import ResponseRequest, RequestTraceContext, client_registry
    from src.llm_models.model_client import ensure_client_type_loaded
    from src.llm_models.request_snapshot import deserialize_context_item_snapshot
    from src.llm_models.payload_content.context_item import ContextItemBuilder, RoleType
    from src.llm_models.payload_content.tool_option import ToolOption
    config = config_manager.get_model_config()
    selected = config.model_task_config.planner.model_list[0]
    model = next(m for m in config.models if m.name == selected)
    provider = next(p for p in config.api_providers if p.name == model.api_provider)
    ensure_client_type_loaded(provider.client_type)
    client = client_registry.get_client_class_instance(provider)
    session = 'eea9ec46d0880b86983de2e4235f4bca'
    async def get(mid, chat_id='', include_binary_data=False):
        message = message_service.get_message_by_id(mid, chat_id=chat_id)
        return PluginMessageUtils._session_message_to_dict(message, include_binary_data=include_binary_data) if message else None
    ctx = SimpleNamespace(message=SimpleNamespace(get_by_id=get))
    results = []
    for name in ['1791561125516.json', '1791561228275.json']:
        doc = json.loads((Path('logs/maisaka_prompt/planner/qq_group_1022382380') / name).read_text())
        checked = await media.available_media(ctx, {'session_id': session, 'items': doc['request_items']})
        targets = [r for r in checked if r['is_drawing_reference']]
        assert targets and all(r['source_message_ids'] == ['1485460619'] for r in targets), '原图被无关聊天挤出'
        guidance = factual_context({}, media=checked)['items'][-1]['parts'][0]['text']
        # 展示快照故意不保存原生 replay；仅在隔离回放中使用可移植公开投影。
        items = [deserialize_context_item_snapshot(i) for i in doc['request_items']
                 if i.get('item_type') not in {'ProviderOpaqueItem', 'ReasoningItem'}]
        items.append(ContextItemBuilder().set_role(RoleType.System).add_text_content(guidance).build())
        options = []
        for definition in doc['tool_definitions']:
            function = definition.get('function', definition)
            options.append(ToolOption(name=function['name'], description=function.get('description', ''),
                parameters_schema_override=function.get('parameters', function.get('parameters_schema'))))
        start = time.monotonic()
        result = await client.get_response(ResponseRequest(model_info=model, context_items=items,
            tool_options=options, max_tokens=4096, trace_context=RequestTraceContext(
                task_name='explicit_quote_replay', request_type='manual.explicit_quote_regression',
                session_id='isolated-explicit-quote-replay')))
        calls = result.tool_calls or []
        allowed_ids = {'1485460619', '569492178', '679943644'}
        direct = [c for c in calls if c.func_name == 'chatgpt_image' and c.args.get('action') in {'edit', 'generate'}
                  and str(c.args.get('message_id')) in allowed_ids]
        analysis = [c for c in calls if c.func_name == 'analyze_task_image' and str(c.args.get('message_id')) in allowed_ids]
        assert direct or analysis, '仍未解析明确引用：' + json.dumps([
            {'tool': c.func_name, 'args': c.args} for c in calls], ensure_ascii=False)[:900]
        results.append({'snapshot': name, 'model': result.generation_trace.model,
                        'seconds': round(time.monotonic() - start, 2), 'passed': True,
                        'target_ids': sorted(allowed_ids), 'direct_edit': bool(direct),
                        'calls': [{'tool': c.func_name, 'args': c.args} for c in calls]})
    path = Path('/mnt/d/code/web/QQbot/data/opencode-go/explicit-quote-verification.json')
    path.write_text(json.dumps({'results': results, 'tools_executed': 0, 'qq_messages_sent': 0}, ensure_ascii=False, indent=2))
    return [{k: r[k] for k in ['snapshot', 'model', 'seconds', 'passed', 'direct_edit']} for r in results]

if __name__ == '__main__':
    try:
        with redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()):
            output = asyncio.run(main())
    except Exception as exc:
        print(json.dumps({'success': False, 'error_type': type(exc).__name__,
                          'check': str(exc)[:950] if isinstance(exc, (AssertionError, ValueError)) else 'replay_failure'}, ensure_ascii=False))
        raise SystemExit(1)
    print(json.dumps(output, ensure_ascii=False))

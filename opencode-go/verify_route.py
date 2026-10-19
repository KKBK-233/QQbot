"""验证 Haiku 主会话服务路由和工具返回；不执行工具或发送群消息。"""
import argparse
import asyncio
from contextlib import redirect_stderr, redirect_stdout
import io
import json
import os
from pathlib import Path
import sys
import time

ROOT = Path('/home/qqbot/QQbot/MaiBot')
MODEL = 'opencode-go-haiku-5.5'


async def verify(preflight=False):
    """预检使用模型覆盖，切换后验证实际任务选择，均只发送合成内容。"""
    os.chdir(ROOT)
    sys.path.insert(0, str(ROOT))
    from src.services import llm_service
    from src.config.config import config_manager
    config = config_manager.get_model_config()
    results = []
    for task in ('planner', 'replyer'):
        prompt = [
            {'role': 'system', 'content': '隔离兼容测试。严格按照用户格式返回。'},
            {'role': 'user', 'content': '只调用 probe_status 一次。' if task == 'planner' else '只回复：伊蕾娜在线'},
            {'role': 'system', 'content': '【宿主本轮上下文】只验证接入，不执行实际操作。'},
        ]
        options = [{'name': 'probe_status', 'description': '隔离状态探针，无外部操作。',
                    'parameters_schema': {'type': 'object', 'properties': {}, 'required': []}}] if task == 'planner' else None
        started = time.monotonic()
        generated = await llm_service.generate(llm_service.LLMServiceRequest(
            task_name=task, request_type='plugin.haiku_route_smoke',
            session_id='qqbot-haiku55-isolated-route', model_name=MODEL if preflight else None,
            prompt=prompt, tool_options=options, max_tokens=512 if task == 'planner' else 80))
        if not generated.success:
            raise RuntimeError('实际服务路由请求失败')
        output = generated.completion
        trace = output.generation_trace
        if trace.provider != 'OpenCodeGo' or trace.model != 'claude-haiku-5-5':
            raise RuntimeError('实际返回模型与 Haiku 配置不一致')
        calls = output.tool_calls or []
        if task == 'planner':
            if len(calls) != 1 or calls[0].func_name != 'probe_status':
                raise RuntimeError('规划器工具调用验证失败')
        elif calls or not output.response or output.response.strip() != '伊蕾娜在线':
            raise RuntimeError('回复器短格式验证失败')
        results.append({'task': task, 'provider': trace.provider, 'model': trace.model,
                        'configured_models': getattr(config.model_task_config, task).model_list,
                        'seconds': round(time.monotonic() - started, 3), 'passed': True,
                        'tools_executed': 0, 'qq_messages_sent': 0, 'memory_writes': 0})
    destination = Path('/mnt/d/code/web/QQbot/data/opencode-go')
    destination.mkdir(parents=True, exist_ok=True)
    (destination / ('preflight.json' if preflight else 'route-verification.json')).write_text(
        json.dumps(results, ensure_ascii=False, indent=2), encoding='utf-8')
    return results


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('--preflight', action='store_true')
    args = parser.parse_args()
    try:
        with redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()):
            result = asyncio.run(verify(args.preflight))
    except Exception as error:
        print(json.dumps({'success': False, 'error_type': type(error).__name__}))
        raise SystemExit(1)
    print(json.dumps(result, ensure_ascii=False))

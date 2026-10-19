"""穿过实际 MaiCore LLMService 配置验证 provider 路由，不执行返回的工具。"""

from pathlib import Path

import asyncio
import argparse
import json
import os
import sys

ROOT = Path('/home/qqbot/QQbot/MaiBot')
os.chdir(ROOT)
sys.path.insert(0, str(ROOT))

from src.llm_models.payload_content.context_item import ContextItemBuilder, RoleType
from src.services import llm_service


async def main(model_id='gpt-6.1-sol') -> None:
    items = [ContextItemBuilder().set_role(RoleType.System).add_text_content(
        '这是隔离的 provider 路由测试。请调用 probe_status 一次，不使用其他工具。').build(),
        ContextItemBuilder().set_role(RoleType.User).add_text_content('检查接入状态。').build()]

    async def context_factory(*args, **kwargs):
        return items

    generated = await llm_service.generate(llm_service.LLMServiceRequest(
        task_name='planner', request_type='plugin.chatgpt_plan_probe', context_factory=context_factory,
        tool_options=[{'name': 'probe_status', 'description': '隔离状态探针，只返回模拟数据。',
                       'parameters_schema': {'type': 'object', 'properties': {}, 'required': []}}],
        max_tokens=1000))
    if not generated.success:
        raise RuntimeError('MaiCore LLMService 请求失败')
    calls = generated.completion.tool_calls or []
    if len(calls) != 1 or calls[0].func_name != 'probe_status':
        raise RuntimeError('实际任务路由没有返回预期函数调用')
    trace = generated.completion.generation_trace
    if trace.provider != 'ChatGPTPlan' or trace.model != model_id:
        raise RuntimeError('实际配置路由没有使用目标 provider/model')
    report = {'success': True, 'task': 'planner', 'provider': trace.provider, 'model': trace.model,
              'tool': calls[0].func_name, 'tools_executed': 0, 'qq_messages_sent': 0, 'memory_writes': 0}
    (ROOT / 'data/chatgpt-plan/service-verification.json').write_text(json.dumps(report, ensure_ascii=False, indent=2))
    print(json.dumps(report, ensure_ascii=False))


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('--model', choices=['gpt-6.1-sol', 'gpt-6-luna'], default='gpt-6.1-sol')
    args = parser.parse_args()
    asyncio.run(main(args.model))

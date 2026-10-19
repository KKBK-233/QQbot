"""顺序回放两个相邻真实快照以验证服务端缓存；只产生短文字，不执行工具或发QQ。"""

from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Dict

import argparse
import asyncio
import io
import json
import os
import sys
import time

ROOT = Path('/home/qqbot/QQbot/MaiBot')
os.chdir(ROOT)
sys.path.insert(0, str(ROOT))

from openai import APIStatusError, AsyncOpenAI
from src.llm_models.model_client.chatgpt_plan_auth import AccountStore, RESOURCE
from src.llm_models.model_client.openai_chatgpt_plan import consume_plan_stream, plan_payload
from src.llm_models.model_client.openai_responses_client import _convert_context_items
from src.llm_models.payload_content.context_item import CONTEXT_ITEM_SCHEMA_VERSION
from src.llm_models.payload_content.context_protocol import ContextProtocolMode
from src.plugin_runtime.hook_payloads import deserialize_prompt_items


def payload(path: Path) -> Dict[str, Any]:
    snapshot = json.loads(path.read_text())
    items = deserialize_prompt_items(snapshot['request_items'], item_schema_version=CONTEXT_ITEM_SCHEMA_VERSION,
                                     mode=ContextProtocolMode.REQUEST_CONTEXT)
    request = SimpleNamespace(model_info=SimpleNamespace(model_identifier='gpt-6.1-sol'))
    inputs = _convert_context_items(items, request, 'ChatGPTPlan', RESOURCE)
    # 原工具定义继续发送以保真缓存前缀；禁止调用，并在末尾明确隔离测试的短输出要求。
    tools = [dict(tool['function'], type='function') for tool in snapshot.get('tool_definitions', [])]
    inputs.append({'role': 'developer', 'content': [{'type': 'input_text',
                   'text': '这是离线缓存验证，不执行以上聊天任务。仅输出OK，不解释、不调用任何工具。'}]})
    return plan_payload(inputs, tools, 'gpt-6.1-sol', {'tool_choice': 'none'})


async def verify(task: str, repeat: bool = False) -> Dict[str, Any]:
    files = sorted((ROOT / 'logs/maisaka_prompt' / task / 'qq_group_1022382380').glob('*.json'))
    files = [path for path in files if json.loads(path.read_text())['metadata']['model_name'] == 'chatgpt-6.1-sol-low'][-2:]
    if len(files) != 2:
        raise RuntimeError('缺少两个相邻的6.1实际请求快照')
    if repeat:
        files = [files[-1], files[-1]]
    bodies = [payload(path) for path in files]
    shared_items = 0
    for first, second in zip(bodies[0]['input'], bodies[1]['input']):
        if first != second:
            break
        shared_items += 1
    token = await asyncio.to_thread(AccountStore().token)
    results = []
    async with AsyncOpenAI(api_key=token, base_url=RESOURCE, timeout=180, max_retries=0) as client:
        for path, body in zip(files, bodies):
            began = time.monotonic()
            try:
                stream = await client.responses.create(**body)
                response = await consume_plan_stream(stream)
            except APIStatusError as error:
                # 只暴露状态与参数名，绝不打印SDK原始异常或模型正文。
                data = error.response.json().get('error', {})
                raise RuntimeError('缓存探针HTTP=' + str(error.status_code) + ' param=' + str(data.get('param'))) from None
            usage = response.get('usage') or {}
            details = usage.get('input_tokens_details') or {}
            results.append({'snapshot': path.stem, 'seconds': round(time.monotonic() - began, 3),
                            'input_tokens': usage.get('input_tokens'), 'cached_tokens': details.get('cached_tokens'),
                            'cache_write_tokens': details.get('cache_write_tokens')})
    # 缓存结果必须结合前缀一致性解读，不能将窗口滚动或插件规则变化当成相同请求。
    report = {'task': task, 'repeat': repeat, 'requests': results,
              'shared_input_items': shared_items, 'tools_equal': bodies[0].get('tools') == bodies[1].get('tools'),
              'entire_payload_equal': bodies[0] == bodies[1],
              'tools_executed': 0, 'qq_messages_sent': 0, 'memory_writes': 0}
    (ROOT / 'data/chatgpt-plan' / ('cache-verification-' + task + ('-repeat' if repeat else '') + '.json')).write_text(json.dumps(report, ensure_ascii=False, indent=2))
    return report


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('--task', choices=['planner', 'replyer'], default='planner')
    parser.add_argument('--repeat', action='store_true')
    args = parser.parse_args()
    try:
        with redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()):
            result = asyncio.run(verify(args.task, args.repeat))
    except RuntimeError as error:
        print(json.dumps({'success': False, 'reason': str(error)}, ensure_ascii=False))
        raise SystemExit(1)
    print(json.dumps(result, ensure_ascii=False))

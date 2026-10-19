"""有限回放DeepSeek请求，只记录传输阶段与耗时，不执行工具或发送QQ消息。"""
from pathlib import Path
import argparse
import asyncio
import json
import os
import sys
import time
import tomllib

import httpx
from openai import AsyncOpenAI


async def main(args):
    """按真实配置连接，关闭诊断重试，区分连接、首响应与正文等待。"""
    root = Path('/home/qqbot/QQbot/MaiBot')
    config = tomllib.loads((root / 'config/model_config.toml').read_text())
    model = next(m for m in config['models'] if m['name'] == 'deepseek-flash')
    provider = next(p for p in config['api_providers'] if p['name'] == model['api_provider'])
    payload = {'messages': [{'role': 'user', 'content': '只回复OK'}], 'max_tokens': 16,
               'extra_body': model.get('extra_params', {})}
    if args.snapshot:
        # 使用宿主转换器保持失败请求的消息与工具定义，仅调用模型而不执行返回的工具。
        os.chdir(root)
        sys.path.insert(0, str(root))
        from src.llm_models.model_client.openai_client import _convert_messages, _convert_tool_options
        from src.llm_models.request_snapshot import deserialize_persisted_context_items_snapshot, deserialize_tool_options_snapshot
        snapshot = json.loads(Path(args.snapshot).read_text())
        params = snapshot['request_parameters']
        payload = {'messages': _convert_messages(deserialize_persisted_context_items_snapshot(snapshot['request_items'])),
                   'tools': _convert_tool_options(deserialize_tool_options_snapshot(snapshot['tool_definitions'])),
                   'max_tokens': params['max_tokens'], 'temperature': params['temperature'],
                   'extra_body': params.get('extra_params', {})}
    print('REQUEST_SHAPE', len(payload['messages']), len(payload.get('tools', [])),
          len(json.dumps(payload, ensure_ascii=False).encode()), 'stream=' + str(args.stream), flush=True)
    print('NETWORK_ROUTE', 'windows_direct_channel' if args.windows_direct else 'wsl_direct' if args.direct else 'local_proxy', flush=True)
    started = time.monotonic()
    async def trace(name, info):
        # 不打印info：其中可能有Authorization、聊天正文或响应内容。
        print('HTTP_PHASE', round(time.monotonic() - started, 3), name, flush=True)
    async def hook(request):
        request.extensions['trace'] = trace
    timeout = httpx.Timeout(args.timeout, connect=8, write=8, pool=3)
    proxy = 'http://127.0.0.1:2082' if args.windows_direct else None if args.direct else 'http://127.0.0.1:2081'
    async with httpx.AsyncClient(proxy=proxy, timeout=timeout, trust_env=False,
                                 event_hooks={'request': [hook]}) as transport:
        client = AsyncOpenAI(api_key=provider['api_key'], base_url=provider['base_url'],
                             http_client=transport, max_retries=0)
        try:
            async with asyncio.timeout(args.timeout + 5):
                response = await client.chat.completions.create(model=model['model_identifier'], stream=args.stream, **payload)
                if args.stream:
                    first = True
                    count = 0
                    async for chunk in response:
                        if first:
                            print('FIRST_CHUNK', round(time.monotonic() - started, 3), flush=True)
                            first = False
                        count += 1
                    print('STREAM_CHUNKS', count, flush=True)
                else:
                    print('RESPONSE_FINISH', response.choices[0].finish_reason, flush=True)
                print('REQUEST_OK', round(time.monotonic() - started, 3), flush=True)
        except Exception as error:
            # 错误类型和状态码足够诊断；不回显异常中的请求、密钥或模型内容。
            print('REQUEST_ERROR', type(error).__name__, getattr(error, 'status_code', None),
                  round(time.monotonic() - started, 3), flush=True)


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--snapshot', help='失败请求快照；留空仅发送最小请求')
    parser.add_argument('--stream', action='store_true', help='只为诊断切换流式响应')
    routes = parser.add_mutually_exclusive_group()
    routes.add_argument('--direct', action='store_true', help='仅诊断WSL原始直连，不走本机通道')
    routes.add_argument('--windows-direct', action='store_true', help='经2082本机通道由Windows连接官方DeepSeek，与当前机器人一致')
    parser.add_argument('--timeout', type=float, default=25)
    args = parser.parse_args()
    if not 1 <= args.timeout <= 55:
        parser.error('诊断等待需在1到55秒之间')
    asyncio.run(main(args))

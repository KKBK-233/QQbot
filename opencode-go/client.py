"""MaiBot 的 OpenCode Go Haiku 5.5 可选客户端；不自动切换生产任务。"""
import asyncio
import hashlib
import json
import time
from pathlib import Path

import httpx

from src.common.logger import get_logger
from src.llm_models.exceptions import NetworkConnectionError, RespNotOkException, RespParseException
from src.llm_models.payload_content.context_item import ContextToolCall, build_portable_output_items
from src.llm_models.model_client.base_client import APIResponse, BaseClient, UsageRecord, client_registry
from src.llm_models.model_client.adapter_base import await_task_with_interrupt
from .opencode_go_cache_layout import with_prompt_cache, record_cache_usage
from .opencode_go_protocol import BASE, MODEL, PROXY, messages_payload, response_parts, response_metadata, thinking_settings
from .opencode_go_native import convert_context, native_item, native_scope, NATIVE_TYPE
from src.llm_models.payload_content.context_item import ProviderOpaqueItem

KEY_FILE = Path.home() / '.config/maibot-opencode-go/api-key'
CACHE_USAGE_FILE = Path('/home/qqbot/QQbot/MaiBot/data/opencode-go/cache-usage.jsonl')
logger = get_logger('opencode_go_transport')


def request_session(trace):
    """聊天沿用宿主会话，独立后台任务使用稳定的任务类型编号。"""
    if trace is None:
        raise ValueError('OpenCode Go必须提供请求追踪上下文')
    if trace.session_id:
        return trace.session_id
    if trace.task_name in {'memory', 'mid_memory', 'utils', 'learner', 'expression_use', 'emoji', 'vlm'}:
        return 'background:' + trace.task_name + ':' + (trace.request_type or 'default')
    raise ValueError('OpenCode Go聊天请求必须提供稳定的会话编号')


@client_registry.register_client_class('opencode_go')
class OpenCodeGoClient(BaseClient):
    """固定套餐端点、真实客户端标识和会话头，禁止重定向或付费回退。"""

    def __init__(self, api_provider):
        if api_provider.base_url.rstrip('/') != BASE or api_provider.default_headers or api_provider.default_query:
            raise ValueError('OpenCode Go只接受固定套餐地址，不接受自定义默认请求头')
        super().__init__(api_provider)
        self._portable_turn_ids = set()

    async def get_response(self, request):
        """使用非流式原生Messages请求，返回宿主统一文本、工具和用量。"""
        if request.model_info.model_identifier != MODEL:
            raise ValueError('此试用客户端只支持Haiku 5.5')
        if request.stream_response_handler or request.async_response_parser or request.model_info.force_stream_mode:
            raise ValueError('试用适配不支持自定义解析器或强制流式')
        if request.response_format:
            raise ValueError('试用适配不支持结构化响应格式')
        settings = thinking_settings(request.model_info.extra_params, request.extra_params)
        trace = request.trace_context
        session_id = request_session(trace)
        if KEY_FILE.stat().st_mode & 0o077:
            raise ValueError('OpenCode凭据文件权限必须为0600')
        key = KEY_FILE.read_text().strip()
        if not key.startswith('oc_sk_'):
            raise ValueError('OpenCode凭据格式无效')
        tools = [tool.to_openai_function_schema()['function'] for tool in request.tool_options or []]
        scope = native_scope(self.api_provider)
        payload = with_prompt_cache(messages_payload(convert_context(request.context_items, scope,
            self._portable_turn_ids), tools, request.max_tokens or 512))
        payload.update(settings)
        if request.model_info.send_temperature and request.temperature is not None:
            payload['temperature'] = request.temperature
        headers = {'x-api-key': key, 'anthropic-version': '2023-06-01',
                   'User-Agent': 'QQbot-OpenCode-Integration/1.0',
                   'x-opencode-session': 'qqbot-' + hashlib.sha256(session_id.encode()).hexdigest()[:32]}

        async def send():
            nonlocal payload
            # 只记录连接阶段、地址与耗时，不记录请求头、正文或服务端错误正文。
            started = time.monotonic()
            phase, peer = 'connect', 'unknown'

            async def transport_trace(name, info):
                nonlocal phase, peer
                if name.startswith('connection.connect_tcp.'):
                    phase = 'tcp'
                    if name.endswith('.complete'):
                        stream = info.get('return_value')
                        if stream is not None:
                            address = stream.get_extra_info('server_addr')
                            if isinstance(address, tuple) and address:
                                peer = str(address[0])
                elif name.startswith('connection.start_tls.'):
                    phase = 'tls'
                elif name.endswith('send_request_headers.started'):
                    phase = 'request'
                elif name.endswith('receive_response_headers.started'):
                    phase = 'response_headers'
                elif name.endswith('receive_response_body.started'):
                    phase = 'response_body'

            # OpenCode 显式走本机代理桥，忽略环境中的代理覆盖和 NO_PROXY；其他供应商自行选路。
            async with httpx.AsyncClient(proxy=PROXY, trust_env=False, follow_redirects=False,
                                         timeout=httpx.Timeout(self.api_provider.timeout, connect=10)) as client:
                try:
                    response = await client.post(BASE + '/messages', headers=headers, json=payload,
                                                 extensions={'trace': transport_trace})
                except httpx.RequestError as error:
                    logger.warning('OpenCode代理连接失败 phase=%s peer=%s seconds=%.2f error=%s' %
                                   (phase, peer, time.monotonic() - started, type(error).__name__))
                    raise NetworkConnectionError('OpenCode连接失败：' + type(error).__name__) from None
                logger.debug('OpenCode代理连接完成 phase=%s peer=%s seconds=%.2f http=%d' %
                             (phase, peer, time.monotonic() - started, response.status_code))
                if response.status_code != 200:
                    # 只提取协议校验词，避免上游回显正文、密钥或思考签名。
                    try:
                        error = response.json().get('error', {})
                        message = str(error.get('message', '')) if isinstance(error, dict) else ''
                    except (ValueError, AttributeError):
                        message = ''
                    terms = [word for word in ('signature', 'thinking', 'tool_use', 'tool_result',
                             'messages', 'content', 'cache_control', 'invalid', 'required', 'expected',
                             'first', 'last', 'duplicate', 'empty', 'image', 'max_tokens', 'budget')
                             if word in message.lower()]
                    logger.warning('OpenCode协议请求失败 http=%d fields=%s' %
                                   (response.status_code, json.dumps(terms)))
                    turns = {it.meta.logical_turn_id for it in request.context_items
                             if isinstance(it, ProviderOpaqueItem) and it.provider_type == NATIVE_TYPE
                             and it.meta.logical_turn_id not in self._portable_turn_ids}
                    if response.status_code == 400 and turns and all(
                            word in terms for word in ('signature', 'thinking', 'invalid')):
                        # 仅签名无效时重试一次；其他400及内容拒绝原样失败，不修改原始历史。
                        portable = with_prompt_cache(messages_payload(convert_context(request.context_items,
                            scope, self._portable_turn_ids | turns), tools, request.max_tokens or 512))
                        for field in ('thinking', 'output_config', 'temperature'):
                            if field in payload:
                                portable[field] = payload[field]
                        retry = await client.post(BASE + '/messages', headers=headers, json=portable)
                        if retry.status_code == 200:
                            self._portable_turn_ids.update(turns)
                            # 缓存只保存回合编号，限制长期运行时占用；新回合仍保留原生思考。
                            if len(self._portable_turn_ids) > 512:
                                self._portable_turn_ids = set(turns)
                            payload = portable
                            logger.warning('OpenCode无效签名回合已改用可见投影，单次重试成功，回合数=%d' % len(turns))
                            return retry.json()
                        logger.warning('OpenCode签名恢复单次重试失败 http=%d' % retry.status_code)
                        raise RespNotOkException(retry.status_code, 'OpenCode签名恢复失败')
                    # 不输出上游原文，避免服务端回显凭据或用户内容。
                    raise RespNotOkException(response.status_code, 'OpenCode套餐请求失败')
                try:
                    return response.json()
                except ValueError:
                    raise RespParseException(message='OpenCode返回了非JSON响应') from None

        data = await await_task_with_interrupt(asyncio.create_task(send()), request.interrupt_flag)
        try:
            text, calls, counts = response_parts(data)
        except (ValueError, KeyError, TypeError):
            raise RespParseException(message='OpenCode返回型号、工具或终止状态不符合约定', ext_info=response_metadata(data)) from None
        output = build_portable_output_items(content=text, logical_turn_id=request.logical_turn_id,
            tool_calls=[ContextToolCall.create(call_id=c['id'], func_name=c['name'], args=c['input']) for c in calls])
        output = [native_item(data['content'], request.logical_turn_id, scope), *output]
        result = APIResponse(output_items=output,
            usage=UsageRecord(model_name=request.model_info.name, provider_name=self.api_provider.name,
                prompt_tokens=counts['prompt'], completion_tokens=counts['output'],
                total_tokens=counts['prompt'] + counts['output'], prompt_cache_hit_tokens=counts['cache_hit'],
                prompt_cache_miss_tokens=counts['cache_miss'], prompt_cache_reported=counts['cache_reported']),
            provider_response=data, wire_protocol='anthropic_messages', request_wire_payload=payload)
        result.attach_generation_trace(provider=self.api_provider.name, endpoint=BASE + '/messages',
                                        model=data['model'], response_id=data.get('id'))
        # 只附加数字遥测；磁盘异常不重试模型、不影响已经生成的回复。
        record_cache_usage(CACHE_USAGE_FILE, data, trace.task_name, headers['x-opencode-session'][6:])
        return result

    async def get_embedding(self, request):
        raise ValueError('OpenCode Go试用不支持嵌入')

    async def get_image_embedding(self, request):
        raise ValueError('OpenCode Go试用不支持图片嵌入')

    async def get_audio_transcriptions(self, request):
        raise ValueError('OpenCode Go试用不支持音频转录')

    def get_support_image_formats(self):
        return ['png', 'jpeg', 'webp', 'gif']

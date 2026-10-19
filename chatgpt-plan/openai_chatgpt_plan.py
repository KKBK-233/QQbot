"""用官方 ChatGPT plan OAuth Responses 替换推理模型，工具仍由 MaiCore 执行。"""

from copy import deepcopy
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence

import asyncio
import hashlib
import json
import logging
import os
import time

from openai import APIConnectionError, APIStatusError, AsyncOpenAI

from src.config.model_configs import APIProvider
from src.llm_models.exceptions import NetworkConnectionError, ReqAbortException, RespNotOkException, RespParseException
from src.llm_models.openai_compat import split_openai_request_overrides

from .adapter_base import await_task_with_interrupt
from .base_client import APIResponse, ResponseRequest, client_registry
from .chatgpt_plan_auth import AccountStore, AuthError, RESOURCE
from .chatgpt_plan_cache_layout import stable_instruction_prefix
from .openai_responses_client import (
    OpenAIResponsesClient,
    _convert_context_items,
    _convert_tool_options,
    _merge_text_config,
    _parse_completed_response,
)

CLIENT_TYPE = "openai_chatgpt_plan"
NAMESPACE = "maibot"
UNSUPPORTED_FIELDS = {
    "background", "conversation", "max_output_tokens", "max_tokens", "max_tool_calls", "metadata",
    "moderation", "multi_agent", "prompt", "prompt_cache_retention", "safety_identifier",
    "temperature", "top_logprobs", "top_p", "truncation", "user", "previous_response_id",
}
ALLOWED_FIELDS = {"reasoning", "include", "parallel_tool_calls", "tool_choice", "prompt_cache_key", "instructions", "text"}
LOGGER = logging.getLogger(__name__)
CACHE_KEY_VERSION = 'maibot-plan-v2'


def record_cache_usage(completed: Dict[str, Any], request: ResponseRequest) -> None:
    """仅记录服务端用量和归属哈希；不保存提示词、工具参数、图片、账号或令牌。"""
    usage = completed.get('usage') or {}
    details = usage.get('input_tokens_details') or {}
    trace = request.trace_context
    entry = {'timestamp': time.time(), 'model': request.model_info.model_identifier,
             'task': trace.task_name if trace else '', 'request_type': trace.request_type if trace else '',
             'session_hash': hashlib.sha256(trace.session_id.encode()).hexdigest()[:16] if trace and trace.session_id else '',
             'input_tokens': usage.get('input_tokens'), 'cached_tokens': details.get('cached_tokens'),
             'cache_write_tokens': details.get('cache_write_tokens'), 'output_tokens': usage.get('output_tokens')}
    # 遥测故障须可见，但不能因此重试或丢掉已经完成的模型输出。
    try:
        folder = Path('/home/qqbot/QQbot/MaiBot/data/chatgpt-plan')
        folder.mkdir(parents=True, exist_ok=True)
        descriptor = os.open(folder / 'cache-usage.jsonl', os.O_CREAT | os.O_APPEND | os.O_WRONLY, 0o600)
        try:
            os.write(descriptor, (json.dumps(entry, ensure_ascii=False) + '\n').encode())
        finally:
            os.close(descriptor)
    except OSError as error:
        LOGGER.warning('缓存用量记录失败 exception=%s', type(error).__name__)


def plan_payload(input_items: Sequence[Dict[str, Any]], tools: Sequence[Dict[str, Any]],
                 model: str, extra: Dict[str, Any]) -> Dict[str, Any]:
    """保留完整上下文及 call_id，转换 system 和工具命名空间，过滤预览不支持的字段。"""
    body = deepcopy(extra)
    for key in UNSUPPORTED_FIELDS:
        body.pop(key, None)
    for key in ["store", "stream", "model", "input"]:
        if key in body:
            raise ValueError(f"{key} 由 ChatGPT provider 管理")
    unknown = set(body) - ALLOWED_FIELDS
    if unknown:
        raise ValueError("ChatGPT plan 不支持额外参数: " + ", ".join(sorted(unknown)))
    inputs = deepcopy(list(input_items))
    for item in inputs:
        if item.get("role") == "system":
            item["role"] = "developer"
        if item.get("type") == "function_call":
            item["namespace"] = NAMESPACE
    inputs = stable_instruction_prefix(inputs)
    functions = []
    for tool in tools:
        if tool.get("type") != "function":
            raise ValueError("此 provider 仅接入 MaiBot 本地 function tools")
        function = deepcopy(tool)
        function["strict"] = False
        functions.append(function)
    # 注册顺序在插件重载后可能变化，按工具名和参数键稳定序列化，不改函数语义。
    functions = sorted((json.loads(json.dumps(function, sort_keys=True, ensure_ascii=False))
                        for function in functions), key=lambda function: function['name'])
    reasoning = body.setdefault("reasoning", {"effort": "low"})
    if reasoning.get("effort", "low") not in {"low", "medium", "high", "xhigh", "max"}:
        raise ValueError("ChatGPT 模型的 reasoning.effort 设置无效")
    include = body.setdefault("include", [])
    if "reasoning.encrypted_content" not in include:
        include.append("reasoning.encrypted_content")
    body.update({"model": model, "input": inputs, "store": False, "stream": True})
    if functions:
        body["tools"] = [{"type": "namespace", "name": NAMESPACE,
                          "description": "MaiBot 本地工具，由 MaiCore 执行并返回结果。", "tools": functions}]
    # 登录预览通道拒绝显式断点，使用稳定缓存键，不注入未经支持的新缓存参数。
    body.setdefault('prompt_cache_key', CACHE_KEY_VERSION + ':default')
    return body


async def consume_plan_stream(stream, interrupt_flag: Optional[asyncio.Event] = None) -> Dict[str, Any]:
    """必须收到 completed 才算成功；按输出位置汇总 done 项，避免完成事件漏掉工具调用。"""
    output: Dict[int, Dict[str, Any]] = {}
    completed = None
    try:
        async for event in stream:
            if interrupt_flag is not None and interrupt_flag.is_set():
                raise ReqAbortException("请求被外部信号中断")
            data = event if isinstance(event, dict) else event.model_dump(mode="json", exclude_none=True)
            kind = data.get("type")
            if kind == "response.output_item.done":
                output[int(data["output_index"])] = data["item"]
            elif kind == "response.completed":
                completed = data["response"]
                if completed.get("status") != "completed":
                    raise RespParseException(message="Responses 完成事件状态无效")
            elif kind in {"response.failed", "response.incomplete", "error"}:
                raise RespParseException(message="ChatGPT Responses 请求未完成: " + str(kind))
    finally:
        await stream.close()
    if completed is None:
        raise RespParseException(message="Responses 流结束但缺少 response.completed")
    if not completed.get("output") and output:
        completed["output"] = [output[index] for index in sorted(output)]
    # 命名空间只影响协议寻址，交回 MaiCore 的工具名保持原名。
    for item in completed.get("output", []):
        if item.get("type") == "function_call":
            namespace = item.get("namespace")
            if namespace and namespace != NAMESPACE:
                raise RespParseException(message="收到未知的工具命名空间")
            if str(item.get("name", "")).startswith(NAMESPACE + "."):
                item["name"] = item["name"][len(NAMESPACE) + 1:]
    return completed


@client_registry.register_client_class(CLIENT_TYPE)
class OpenAIChatGPTPlanClient(OpenAIResponsesClient):
    """复用 MaiBot 上下文与原生响应解析，独立管理 OAuth 与预览协议。"""

    def __init__(self, api_provider: APIProvider) -> None:
        if api_provider.base_url.rstrip("/") != RESOURCE:
            raise ValueError("ChatGPT plan 只能调用官方 https://api.openai.com/v1")
        if api_provider.default_headers or api_provider.default_query or api_provider.organization or api_provider.project:
            raise ValueError("ChatGPT plan 不使用 API 项目鉴权或自定义默认头")
        super().__init__(api_provider)
        self.accounts = AccountStore()

    async def get_response(self, request: ResponseRequest) -> APIResponse:
        if request.stream_response_handler or request.async_response_parser:
            raise RespParseException(message="ChatGPT plan 不支持自定义原始流解析器")
        converted = _convert_context_items(request.context_items, request, self.api_provider.name, RESOURCE)
        overrides = split_openai_request_overrides(request.extra_params)
        if overrides.extra_headers or overrides.extra_query:
            raise ValueError("ChatGPT plan 不接受自定义鉴权请求头或查询参数")
        extra = deepcopy(overrides.extra_body)
        trace = request.trace_context
        task_key = trace.task_name if trace and trace.task_name in {'planner', 'replyer'} else 'default'
        extra.setdefault('prompt_cache_key', CACHE_KEY_VERSION + ':' + task_key)
        text = _merge_text_config(extra, request.response_format)
        if text:
            extra["text"] = text
        payload = plan_payload(converted, _convert_tool_options(request.tool_options),
                               request.model_info.model_identifier, extra)
        try:
            # 文件锁和同步 OAuth 网络请求放入线程，主循环和 WebUI 循环均不阻塞。
            token = await await_task_with_interrupt(asyncio.create_task(asyncio.to_thread(self.accounts.token)),
                                                    request.interrupt_flag)
            # 每次请求绑定当次账号令牌，避免并发切换账号时共享 SDK 凭据。
            async with AsyncOpenAI(api_key=token, base_url=RESOURCE, timeout=self.api_provider.timeout,
                                   max_retries=0, default_headers={"User-Agent": "MaiBot-QQbot/1.13.0"}) as client:
                stream = await await_task_with_interrupt(asyncio.create_task(client.responses.create(**payload)),
                                                         request.interrupt_flag)
                consume = asyncio.create_task(consume_plan_stream(stream, request.interrupt_flag))
                completed = await await_task_with_interrupt(consume, request.interrupt_flag)
            await asyncio.to_thread(record_cache_usage, completed, request)
            response, usage = _parse_completed_response(completed, request, self.api_provider.name,
                                                        RESOURCE, self.tool_argument_parse_mode)
            if usage is not None:
                response.usage = self._build_usage_record(request.model_info, usage)
            response.wire_protocol = "responses"
            response.request_wire_payload = payload
            return response
        except AuthError as exc:
            raise RespNotOkException(401, str(exc)) from None
        except APIStatusError as exc:
            # 不写原始 Authorization、token 响应或 SDK 原始异常。
            raise RespNotOkException(exc.status_code, f"ChatGPT Responses HTTP {exc.status_code}") from None
        except APIConnectionError:
            raise NetworkConnectionError("ChatGPT Responses 网络连接失败") from None

    async def get_embedding(self, request):
        raise ValueError("ChatGPT plan 登录不支持 embeddings；请为嵌入保留独立模型")

    async def get_audio_transcriptions(self, request):
        raise ValueError("ChatGPT plan 登录不支持音频转录；请保留独立模型")

    async def get_image_embedding(self, request):
        raise ValueError("ChatGPT plan 登录不支持图片嵌入；请保留独立模型")

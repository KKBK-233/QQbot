"""利用宿主不透明回放项保存原生思考，避免改动MaiBot核心历史格式。"""
from hashlib import sha256

from src.common.logger import get_logger
from src.llm_models.model_client.openai_client import _convert_messages
from src.llm_models.payload_content.context_item import (
    AssistantMessageItem, ContextItemMeta, FunctionCallItem, ProviderActivityItem,
    ProviderOpaqueItem, ProviderReplayFragment, ProviderScope, ReasoningItem,
    get_response_text,
)
from .opencode_go_protocol import BASE, MODEL, validate_native_blocks

logger = get_logger('opencode_go_native')

NATIVE_TYPE = 'opencode_go_assistant_content'
OUTPUT_TYPES = (AssistantMessageItem, FunctionCallItem, ProviderActivityItem, ProviderOpaqueItem, ReasoningItem)


def native_scope(provider):
    """限定为相同供应商、端点和模型，禁止把签名回放到其他通道。"""
    return ProviderScope(schema_version=1, client_type='opencode_go', provider_name=provider.name,
                         endpoint_fingerprint=sha256((BASE + '/messages').encode()).hexdigest(), model_identifier=MODEL)


def native_item(content, logical_turn_id, scope):
    """深层不可变存储原生内容；展示摘要为空，群聊正文仍来自可移植项。"""
    validate_native_blocks(content)
    return ProviderOpaqueItem(meta=ContextItemMeta.create(logical_turn_id=logical_turn_id),
                              provider_type=NATIVE_TYPE,
                              replay=ProviderReplayFragment.from_payload(scope, {'content': content}))


def convert_context(items, scope, portable_turn_ids=None):
    """按原生响应边界回放；其他供应商和旧历史使用原有可移植投影。"""
    messages = []
    index = 0
    while index < len(items):
        item = items[index]
        if not isinstance(item, OUTPUT_TYPES):
            messages.extend(_convert_messages([item]))
            index += 1
            continue
        turn_id = item.meta.logical_turn_id
        group = [item]
        index += 1
        while index < len(items) and isinstance(items[index], OUTPUT_TYPES) and items[index].meta.logical_turn_id == turn_id:
            group.append(items[index])
            index += 1
        native = [it for it in group if isinstance(it, ProviderOpaqueItem) and it.provider_type == NATIVE_TYPE]
        if not native:
            messages.extend(_convert_messages(group))
            continue
        if len(native) != 1 or native[0].replay is None or native[0].replay.scope != scope:
            raise ValueError('原生回放范围不匹配或重复')
        content = native[0].replay.materialize()['content']
        validate_native_blocks(content)
        text = '\n'.join(p['text'] for p in content if p['type'] == 'text')
        calls = [(p['id'], p['name'], p['input']) for p in content if p['type'] == 'tool_use']
        projected = [(it.tool_call.call_id, it.tool_call.func_name, it.tool_call.materialize_args())
                     for it in group if isinstance(it, FunctionCallItem)]
        if text.strip() != get_response_text(group).strip() or calls != projected:
            if not calls and not projected:
                # 旧插件可能已经删掉纯文字投影；只采用宿主仍保留的正文，不恢复旧分析。
                logger.warning('纯文字回合的原生内容与可见文本不一致，已降级为可移植回放: turn=%s' % turn_id)
                messages.extend(_convert_messages(group))
                continue
            raise ValueError('原生回放与可见文本或工具已不一致')
        if turn_id in (portable_turn_ids or set()):
            # 上游拒绝签名后沿用已核对的可见投影，保留工具编号、参数和结果配对。
            messages.extend(_convert_messages([it for it in group if not isinstance(it, ProviderOpaqueItem)]))
            continue
        messages.append({'role': 'assistant', 'native_content': content})
    return messages

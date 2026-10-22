"""验证官方 DeepSeek 思考工具链的字段回传与原始响应诊断，不调用外部接口。"""

from types import SimpleNamespace

import json
import pytest

from src.config.model_configs import APIProvider, ModelInfo, ReasoningParseMode, ToolArgumentParseMode
from src.llm_models.model_client.base_client import ResponseRequest
from src.llm_models.model_client.openai_client import OpenaiClient, _chat_response_diagnostic, _convert_messages
from src.llm_models.payload_content.context_item import (
    ContextItemBuilder, ContextItemMeta, ContextToolCall, FunctionCallOutputItem,
    ReasoningItem, ReasoningRepresentation, build_portable_output_items,
)
from src.llm_models.payload_content.tool_option import ToolOption


def history():
    """包含工具调用轮次与普通回答轮次，两种历史都要保留各自的原生思考。"""
    return [
        ContextItemBuilder().add_text_content("查找资料").build(),
        *build_portable_output_items(
            reasoning="原生思考一", logical_turn_id="turn-one",
            tool_calls=[ContextToolCall.create(call_id="call-one", func_name="web_search", args={})],
        ),
        FunctionCallOutputItem(meta=ContextItemMeta.create(logical_turn_id="turn-one"), call_id="call-one", output="真实工具资料"),
        *build_portable_output_items(reasoning="原生思考二", content="第一轮结论", logical_turn_id="turn-two"),
        ContextItemBuilder().add_text_content("继续核实").build(),
    ]


@pytest.mark.asyncio
@pytest.mark.parametrize("base_url,thinking,has_tools,expected", [
    ("https://api.deepseek.com", "enabled", True, True),
    ("https://api.deepseek.com/v1", None, True, True),
    ("https://api.deepseek.com", "disabled", True, False),
    ("https://api.deepseek.com", "enabled", False, False),
    ("https://example.com/v1", "enabled", True, False),
    ("https://api.deepseek.com.example.com", "enabled", True, False),
])
async def test_request_replays_reasoning_only_for_official_deepseek_thinking_tools(
    base_url, thinking, has_tools, expected,
):
    client = object.__new__(OpenaiClient)
    client.api_provider = APIProvider(name="offline", base_url=base_url, auth_type="none", client_type="openai")
    client.reasoning_parse_mode = ReasoningParseMode.AUTO
    client.tool_argument_parse_mode = ToolArgumentParseMode.STRICT
    client.reasoning_key = "reasoning_content"
    captured = []

    async def create(**kwargs):
        captured.append(kwargs)
        return SimpleNamespace(model="offline", usage=None, choices=[SimpleNamespace(
            finish_reason="stop", message=SimpleNamespace(content="回复正文", tool_calls=None),
        )])

    client.client = SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(create=create)))
    request = ResponseRequest(
        model_info=ModelInfo(name="offline", model_identifier="deepseek-flash", api_provider="offline"),
        context_items=history(),
        tool_options=[ToolOption(name="web_search", description="搜索")] if has_tools else None,
        extra_params={"thinking": {"type": thinking}} if thinking else {},
    )
    await client.get_response(request)
    assert len(captured) == 1
    messages = captured[0]["messages"]
    assistants = [message for message in messages if message["role"] == "assistant"]
    if expected:
        assert [message.get("reasoning_content") for message in assistants] == ["原生思考一", "原生思考二"]
        assert assistants[0]["content"] is None and assistants[0]["tool_calls"][0]["id"] == "call-one"
        assert assistants[1]["content"] == "第一轮结论"
        assert next(message for message in messages if message["role"] == "tool")["tool_call_id"] == "call-one"
    else:
        assert all("reasoning_content" not in message for message in messages)


def test_reasoning_summary_is_not_replayed_as_native_thought():
    summary = ReasoningItem(
        meta=ContextItemMeta.create(), summary_parts=("只是一段摘要",),
        representation=ReasoningRepresentation.SUMMARY,
    )
    assert _convert_messages([summary], include_reasoning=True) == []
    assert all("reasoning_content" not in message for message in _convert_messages(history()))


def test_raw_diagnostics_only_keep_lengths_and_field_kind():
    raw = SimpleNamespace(choices=[SimpleNamespace(finish_reason="stop", message=SimpleNamespace(
        content=None, reasoning_content="仅留在响应内的原始思考", tool_calls=None,
    ))])
    diagnostic = _chat_response_diagnostic(raw)
    message = diagnostic["choices"][0]["message"]
    assert message["content_kind"] == "none" and message["content_chars"] == 0
    assert message["reasoning_chars"] == len(raw.choices[0].message.reasoning_content)
    assert "仅留在响应内" not in json.dumps(diagnostic, ensure_ascii=False)


def test_deepseek_tool_call_without_thought_preserves_explicit_empty_field():
    """接口没有生成思考时回传空字段，不编造思考，也不遗漏续接字段。"""
    items = build_portable_output_items(
        logical_turn_id="empty-thought",
        tool_calls=[ContextToolCall.create(call_id="empty-call", func_name="web_search", args={})],
    )
    message = _convert_messages(list(items), include_reasoning=True)[0]
    assert message["reasoning_content"] == ""
    assert message["content"] is None and message["tool_calls"][0]["id"] == "empty-call"

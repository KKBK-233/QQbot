"""缓存前缀、可替换身份、结构化错误及服务教学边界回归。"""

from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock
import ast
import asyncio
import sqlite3

import pytest

from src.kkbk_engine.config import IdentityConfig
from src.kkbk_engine.model_errors import error_kind, retryable
from src.kkbk_engine.prompt import build_messages, build_system_prompt, render_turn_block
from src.kkbk_engine.teaching import TeachingMemory
from src.llm_models.exceptions import ModelAttemptFailed, RespNotOkException
from src.services.memory_teaching_repository import load_teachings

from test_kkbk_workflow_runtime import runtime_harness, look, response, ToolResult
from test_kkbk_workflows import setup_files


def prepared_runtime(tmp_path):
    runtime, _ = runtime_harness(tmp_path)
    runtime._prepare = type(runtime)._prepare.__get__(runtime)
    runtime._teaching_guidance = type(runtime)._teaching_guidance.__get__(runtime)
    runtime._development = SimpleNamespace(
        teaching=SimpleNamespace(guidance=AsyncMock(return_value="已保存的搜索教学"))
    )
    runtime.messages = {}
    runtime.history.render = lambda: "固定历史内容"
    ns = type(runtime)._prepare.__globals__
    ns.update(
        build_messages=build_messages,
        build_system_prompt=build_system_prompt,
        render_turn_block=render_turn_block,
        global_config=SimpleNamespace(
            personality=SimpleNamespace(personality="人设", behavior_style="行为", reply_style="语气"),
            chat=SimpleNamespace(reply_style=SimpleNamespace(group_chat_prompt="群聊风格")),
        ),
        serialize_prompt_items=lambda _: [],
        serialize_tool_definitions=lambda specs: specs,
        get_plugin_runtime_manager=lambda: SimpleNamespace(
            invoke_hook=AsyncMock(return_value=SimpleNamespace(kwargs={"items": []}))
        ),
    )
    current = look()
    current.mention_kind = "at"
    return runtime, current


def test_dynamic_tool_lists_do_not_change_system_or_history_prefix(tmp_path):
    runtime, current = prepared_runtime(tmp_path)
    ordinary = runtime._registry.list_tools.return_value
    owner = ordinary + [SimpleNamespace(name="development_submit", to_llm_definition=lambda: {})]
    first = asyncio.run(runtime._prepare(current, ordinary))
    second = asyncio.run(runtime._prepare(current, owner))
    assert first[0].text == second[0].text
    assert "本轮可用工具：" not in first[0].text
    assert first[1].text.split("<本轮情况>")[0] == second[1].text.split("<本轮情况>")[0]
    assert "development_submit" in second[1].text and "development_submit" not in first[1].text
    runtime._development.teaching.guidance.assert_not_awaited()


def test_light_chat_skips_teaching_and_heavy_requests_keep_light_prefix(tmp_path):
    setup_files(tmp_path)
    runtime, current = prepared_runtime(tmp_path)
    runtime._llm.generate_response_with_context.return_value = response(text="聊天")
    asyncio.run(runtime._turn(current))
    runtime._development.teaching.guidance.assert_not_awaited()
    requests = []

    async def light(factory, options):
        requests.append(factory(None))
        return response("escalate", text="", task="搜索")

    async def heavy(factory, options):
        requests.append(factory(None))
        return response(text="完成")

    runtime._llm.generate_response_with_context.side_effect = light
    runtime._tool_llm.generate_response_with_context.side_effect = heavy
    asyncio.run(runtime._turn(current))
    prefix = [(item.role, item.text) for item in requests[0]]
    assert [(item.role, item.text) for item in requests[1][: len(prefix)]] == prefix
    assert any(item.role == "user" and item.text == "已保存的搜索教学" for item in requests[1])
    assert any("本轮固定工作流" in item.text for item in requests[1][len(prefix) :])
    runtime._development.teaching.guidance.assert_awaited_once()


def test_operator_change_updates_teacher_prompt_and_storage_authority():
    identity = IdentityConfig(bot_user_id="new-bot", operator_user_ids=frozenset({"new-owner"}))
    prompt = build_system_prompt("", identity, {}, "")
    assert "new-owner" in prompt and "new-bot" in prompt
    assert "1020050112" not in prompt and "3976405558" not in prompt
    memory = TeachingMemory(SimpleNamespace(), identity=identity)
    with pytest.raises(PermissionError):
        asyncio.run(memory.learn({"platform": "qq", "user_id": "1020050112"}, {}, {}))
    assert IdentityConfig(operator_user_ids=frozenset({"a", "b"})).method_teacher_id is None
    assert IdentityConfig(operator_user_ids=frozenset({"a", "b"}), teacher_user_id="b").method_teacher_id == "b"
    with pytest.raises(ValueError):
        _ = IdentityConfig(operator_user_ids=frozenset({"a"}), teacher_user_id="other").method_teacher_id


def test_inbound_at_fallback_uses_supplied_bot_identity():
    from datetime import datetime
    from src.kkbk_engine.inbound import InboundMessage

    source = Path(__file__).resolve().parents[2] / "src/kkbk_engine/runtime.py"
    node = next(
        node
        for node in ast.parse(source.read_text(encoding="utf-8")).body
        if isinstance(node, ast.FunctionDef) and node.name == "from_session_message"
    )
    ns = {
        "InboundMessage": InboundMessage,
        "IdentityConfig": IdentityConfig,
        "AtComponent": type("At", (), {}),
        "TextComponent": type("Text", (), {}),
        "ReplyComponent": type("Reply", (), {}),
    }
    exec(compile(ast.Module(body=[node], type_ignores=[]), str(source), "exec"), ns)
    message = SimpleNamespace(
        message_id="m",
        timestamp=datetime.now(),
        is_at=True,
        reply_to=None,
        is_notify=False,
        is_command=False,
        processed_plain_text="",
        message_info=SimpleNamespace(user_info=SimpleNamespace(user_id="u", user_nickname="", user_cardname="")),
        raw_message=SimpleNamespace(components=[], to_dict=lambda: []),
    )
    assert ns["from_session_message"](message, bot_user_id="new-bot").mentions == ("new-bot",)


@pytest.mark.parametrize(
    "code,expected", [(400, False), (401, False), (402, False), (403, False), (429, True), (503, True)]
)
def test_structured_wrapped_http_errors(code, expected):
    error = ModelAttemptFailed("模型轮次失败", original_exception=RespNotOkException(code))
    assert retryable(error) == expected
    assert error_kind(error) == ("quota" if code == 402 else "transient" if expected else "request")


def test_numbers_in_network_error_do_not_masquerade_as_http_status():
    assert retryable(OSError("连接 10.0.0.1:4001 失败；余额查询地址不可用"))
    assert not retryable(RuntimeError("unknown failure"))
    error = RespNotOkException(429)
    error.body = {"error": {"code": "insufficient_quota"}}
    wrapped = ModelAttemptFailed("重试耗尽", original_exception=error)
    assert error_kind(wrapped) == "quota" and not retryable(wrapped)


def test_quota_exhaustion_keeps_existing_search_results(tmp_path):
    runtime, _ = runtime_harness(tmp_path / "not-installed")
    runtime._llm.generate_response_with_context.return_value = response("escalate", text="", task="检索")
    call = SimpleNamespace(func_name="web_search", args={}, call_id="s")
    runtime._tool_llm.generate_response_with_context.side_effect = [response(call=call), RespNotOkException(402)]
    runtime._invoke.return_value = ToolResult(content="1. 已有来源\n真实资料摘要\n来源：https://example.com/source")
    asyncio.run(runtime._turn(look()))
    sent = runtime._send.await_args.args[0].text
    assert "额度或余额不足" in sent and "https://example.com/source" in sent
    assert len(sent) <= runtime.config.reply.max_chars
    assert runtime._tool_llm.generate_response_with_context.await_count == 2
    runtime._invoke.assert_awaited_once()


def test_teaching_storage_missing_or_changed_schema_is_explicit(tmp_path):
    with pytest.raises(RuntimeError, match="尚未准备"):
        load_teachings(tmp_path / "missing.db", "teacher")
    path = tmp_path / "changed.db"
    with sqlite3.connect(path) as db:
        db.execute("create table unrelated(value)")
    with pytest.raises(sqlite3.OperationalError):
        load_teachings(path, "teacher")

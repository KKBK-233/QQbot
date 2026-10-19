"""离线执行真实运行时方法，隔离宿主登录、数据库、模型网络及发送服务。"""

from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import ast
import asyncio
import json
import time

import pytest

from src.kkbk_engine.actions import ActionError, ModelAction, ValidationContext, parse_action, validate_action
from src.kkbk_engine.config import EngineConfig
from src.kkbk_engine.gate import LookOutcome
from src.kkbk_engine.workflows import build_workflow_guidance, load_workflow_snapshot

from test_kkbk_workflows import setup_files


def runtime_harness(root):
    # 直接编译生产方法，避免导入 runtime 时启动宿主配置/日志等全局设施。
    path = Path(__file__).resolve().parents[2] / "src/kkbk_engine/runtime.py"
    source = ast.parse(path.read_text(encoding="utf-8"))
    plugin_tools = next(node for node in source.body if isinstance(node, ast.Assign)
                        and any(isinstance(target, ast.Name) and target.id == "PLUGIN_TOOLS"
                                for target in node.targets))
    klass = next(node for node in source.body if isinstance(node, ast.ClassDef)
                 and node.name == "KKBKHeartFlowChatting")
    klass.body = [node for node in klass.body if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
                  and node.name in {"_turn", "_generate", "_retryable"}]
    logger = SimpleNamespace(info=Mock(), error=Mock(), warning=Mock())
    namespace = {
        "asyncio": SimpleNamespace(to_thread=asyncio.to_thread, sleep=AsyncMock()),
        "time": time, "json": json, "logger": logger, "MAX_INTERNAL_ROUNDS": 10,
        "load_workflow_snapshot": lambda: load_workflow_snapshot(root),
        "build_workflow_guidance": build_workflow_guidance,
        "ValidationContext": ValidationContext, "ModelAction": ModelAction, "LookOutcome": LookOutcome,
        "ActionError": ActionError, "parse_action": parse_action, "validate_action": validate_action,
        "LLMGenerationOptions": lambda **kwargs: kwargs, "RespFormat": lambda **kwargs: kwargs,
        "RespFormatType": SimpleNamespace(JSON_OBJ="json"),
        "RoleType": SimpleNamespace(System="system", User="user"),
        "text_item": lambda role, text: SimpleNamespace(role=role, text=text),
        "FunctionCallItem": ToolCallItem,
        "ContextItemMeta": SimpleNamespace(create=lambda **kwargs: kwargs),
        "FunctionCallOutputItem": lambda **kwargs: SimpleNamespace(**kwargs),
    }
    # 一并编译实际名称白名单，避免测试自行放宽工具选择。
    exec(compile(ast.Module(body=[plugin_tools, klass], type_ignores=[]), str(path), "exec"), namespace)
    runtime = namespace["KKBKHeartFlowChatting"]()
    runtime.session_id = "offline-session"
    runtime.log_prefix = "offline"
    runtime.config = EngineConfig()
    runtime._anchor = lambda look: None
    runtime._availability = lambda anchor: None
    specs = [SimpleNamespace(name=name, provider_type="plugin", to_llm_definition=lambda: {})
             for name in ["web_search", "read_webpage"]]
    runtime._registry = SimpleNamespace(list_tools=AsyncMock(return_value=specs))
    runtime._prepare = AsyncMock(side_effect=lambda *_: [SimpleNamespace(role="system", text="原有人设"),
                                                       SimpleNamespace(role="user", text="原始问题")])
    runtime.history = SimpleNamespace(quotable=lambda: {})
    runtime.gate = SimpleNamespace(complete=Mock())
    runtime._send = AsyncMock()
    runtime._after_turn = AsyncMock()
    runtime._llm = SimpleNamespace(generate_response_with_context=AsyncMock())
    runtime._tool_llm = SimpleNamespace(generate_response_with_context=AsyncMock())
    runtime._invoke = AsyncMock()
    return runtime, logger


class ToolCallItem:
    def __init__(self, call):
        self.tool_call = call
        self.meta = SimpleNamespace(logical_turn_id="offline-turn")


def response(action="reply", *, text="完成", task="", call=None):
    return SimpleNamespace(response=json.dumps({"action": action, "track": "leave", "text": text, "task": task}),
                           tool_calls=[call] if call else [], output_items=[ToolCallItem(call)] if call else [],
                           model_name="offline", prompt_tokens=10, completion_tokens=10, prompt_cache_hit_tokens=0)


def look():
    return SimpleNamespace(trigger_message_id="m1", requires_reply=True, no_quote_user_ids=frozenset(), reason="mention")


def test_in_flight_tools_and_retry_keep_snapshot_and_next_turn_reloads(tmp_path):
    setup_files(tmp_path)
    runtime, logger = runtime_harness(tmp_path)
    light_requests, heavy_requests = [], []

    async def light(callback, options):
        light_requests.append(callback(None))
        return response("escalate", text="", task="查证原始问题")

    call = SimpleNamespace(func_name="web_search", args={"query": "原始问题"}, call_id="call1")
    heavy_results = [response(call=call), RuntimeError("temporary failure"), response()]

    async def heavy(callback, options):
        heavy_requests.append(callback(None))
        value = heavy_results.pop(0)
        if isinstance(value, Exception):
            raise value
        return value

    async def invoke(*args):
        setup_files(tmp_path, version="1.1.0", step="仅后续任务的新指导")
        return SimpleNamespace(success=True, metadata={}, get_history_content=lambda: "实际结果")

    runtime._llm.generate_response_with_context.side_effect = light
    runtime._tool_llm.generate_response_with_context.side_effect = heavy
    runtime._invoke.side_effect = invoke
    asyncio.run(runtime._turn(look()))
    assert len(light_requests) == 1 and len(heavy_requests) == 3
    assert all("工作流" not in item.text for item in light_requests[0])
    guidance = [next(item.text for item in request if getattr(item, "role", None) == "system"
                     and "本轮固定工作流" in item.text) for request in heavy_requests]
    assert len(set(guidance)) == 1 and "research@1.0.0" in guidance[0]
    assert "仅后续任务的新指导" not in guidance[0]
    assert runtime._invoke.await_count == 1
    assert runtime._send.await_count == 1
    runtime._llm.generate_response_with_context.side_effect = None
    runtime._llm.generate_response_with_context.return_value = response(text="普通聊天")
    asyncio.run(runtime._turn(look()))
    records = [json.loads(call.args[0]) for call in logger.info.call_args_list]
    snapshots = [record for record in records if record["event"] == "workflow_snapshot"]
    assert [record["workflows"][0]["version"] for record in snapshots] == ["1.0.0", "1.1.0"]
    assert runtime._tool_llm.generate_response_with_context.await_count == 3


@pytest.mark.parametrize("action", ["reply", "escalate"])
def test_bad_config_allows_daily_reply_but_stops_tools(tmp_path, action):
    (tmp_path / "enabled.json").write_text("bad config", encoding="utf-8")
    runtime, logger = runtime_harness(tmp_path)
    runtime._llm.generate_response_with_context.return_value = response(action, task="搜索" if action == "escalate" else "")
    asyncio.run(runtime._turn(look()))
    runtime._send.assert_awaited_once()
    sent = runtime._send.await_args.args[0]
    assert sent.text == "完成" if action == "reply" else "配置未通过校验" in sent.text
    runtime._tool_llm.generate_response_with_context.assert_not_awaited()
    runtime._invoke.assert_not_awaited()
    assert logger.error.call_count == 1


def test_missing_legacy_directory_preserves_tool_path(tmp_path):
    runtime, _ = runtime_harness(tmp_path / "not-installed")
    runtime._llm.generate_response_with_context.return_value = response("escalate", text="", task="搜索")
    runtime._tool_llm.generate_response_with_context.return_value = response()
    asyncio.run(runtime._turn(look()))
    runtime._tool_llm.generate_response_with_context.assert_awaited_once()

"""离线执行真实运行时方法，隔离宿主登录、数据库、模型网络及发送服务。"""

from dataclasses import replace
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
from src.kkbk_engine.guards import ConcurrencyGate, QueueFullError, QueueTimeoutError, SlidingWindowLimiter
from src.kkbk_engine.search_evidence import SearchEvidence
from src.kkbk_engine.model_errors import error_kind, failure_text, retryable
from src.kkbk_engine.workflows import build_workflow_guidance, load_workflow_snapshot

from test_kkbk_workflows import setup_files


def configure_runtime(runtime, root):
    """给编译出的运行时实例补齐离线依赖；类级工具限速器保持真实共享范围。"""
    runtime.session_id = "offline-session"
    runtime.log_prefix = "offline"
    runtime.config = EngineConfig()
    runtime._anchor = lambda look: None
    runtime._availability = lambda anchor: None
    specs = [
        SimpleNamespace(name=name, provider_type="plugin", to_llm_definition=lambda: {})
        for name in ["web_search", "read_webpage"]
    ]
    runtime._registry = SimpleNamespace(list_tools=AsyncMock(return_value=specs))
    runtime._prepare = AsyncMock(
        side_effect=lambda *_: [
            SimpleNamespace(role="system", text="原有人设"),
            SimpleNamespace(role="user", text="原始问题"),
        ]
    )
    runtime._teaching_guidance = AsyncMock(return_value="")
    runtime.history = SimpleNamespace(quotable=lambda: {})
    runtime.gate = SimpleNamespace(complete=Mock())
    runtime._send = AsyncMock()
    runtime._after_turn = AsyncMock()
    runtime._llm = SimpleNamespace(generate_response_with_context=AsyncMock())
    runtime._tool_llm = SimpleNamespace(generate_response_with_context=AsyncMock())
    runtime._invoke = AsyncMock()
    runtime._tool_gates = {}
    runtime.chat_stream = SimpleNamespace(platform="qq", group_id="offline-group")
    return runtime


def runtime_harness(root):
    # 直接编译生产方法，避免导入 runtime 时启动宿主配置/日志等全局设施。
    path = Path(__file__).resolve().parents[2] / "src/kkbk_engine/runtime.py"
    source = ast.parse(path.read_text(encoding="utf-8"))
    # 同时编译生产混入类，测试实际继承的方法，不复制工具执行实现。
    mixins = []
    for filename in ("request_context.py", "tool_execution.py"):
        module = ast.parse(path.with_name(filename).read_text(encoding="utf-8"))
        mixins.append(next(node for node in module.body if isinstance(node, ast.ClassDef)))
    klass = next(
        node for node in source.body if isinstance(node, ast.ClassDef) and node.name == "KKBKHeartFlowChatting"
    )
    body = list(klass.body)
    klass.body = [
        node
        for node in body
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
        and node.name in {"_turn", "_generate", "_retryable", "_invoke", "_wait_for_task", "_run", "_after_turn"}
    ]
    # 保留生产代码的类级限速器，跨任务/跨会话配额测试才能覆盖真实共享范围。
    klass.body.extend(
        node
        for node in body
        if isinstance(node, ast.Assign)
        and any(isinstance(target, ast.Name) and target.id == "_tool_limiter" for target in node.targets)
    )
    for owner in [klass, *mixins]:
        for method in owner.body:
            if isinstance(method, (ast.FunctionDef, ast.AsyncFunctionDef)):
                method.body = [node for node in method.body if not isinstance(node, ast.ImportFrom)]
    logger = SimpleNamespace(info=Mock(), error=Mock(), warning=Mock(), exception=Mock())
    namespace = {
        "asyncio": SimpleNamespace(
            to_thread=asyncio.to_thread,
            sleep=AsyncMock(),
            CancelledError=asyncio.CancelledError,
            TimeoutError=asyncio.TimeoutError,
            wait_for=asyncio.wait_for,
        ),
        "time": time,
        "json": json,
        "logger": logger,
        "MAX_INTERNAL_ROUNDS": 10,
        "load_workflow_snapshot": lambda: load_workflow_snapshot(root),
        "build_workflow_guidance": build_workflow_guidance,
        "ValidationContext": ValidationContext,
        "ModelAction": ModelAction,
        "LookOutcome": LookOutcome,
        "ActionError": ActionError,
        "parse_action": parse_action,
        "validate_action": validate_action,
        "LLMGenerationOptions": lambda **kwargs: kwargs,
        "RespFormat": lambda **kwargs: kwargs,
        "RespFormatType": SimpleNamespace(JSON_OBJ="json"),
        "RoleType": SimpleNamespace(System="system", User="user"),
        "text_item": lambda role, text: SimpleNamespace(role=role, text=text),
        "FunctionCallItem": ToolCallItem,
        "ContextItemMeta": SimpleNamespace(create=lambda **kwargs: kwargs),
        "FunctionCallOutputItem": lambda **kwargs: SimpleNamespace(**kwargs),
        "ToolExecutionResult": ToolResult,
        "ToolInvocation": lambda **kwargs: SimpleNamespace(**kwargs),
        "ToolExecutionContext": lambda **kwargs: SimpleNamespace(**kwargs),
        "ConcurrencyGate": ConcurrencyGate,
        "QueueFullError": QueueFullError,
        "QueueTimeoutError": QueueTimeoutError,
        "SearchEvidence": SearchEvidence,
        "SlidingWindowLimiter": SlidingWindowLimiter,
        "error_kind": error_kind,
        "failure_text": failure_text,
        "retryable": retryable,
    }
    # 一并编译实际名称白名单，避免测试自行放宽工具选择。
    exec(compile(ast.Module(body=[*mixins, klass], type_ignores=[]), str(path), "exec"), namespace)
    runtime = namespace["KKBKHeartFlowChatting"]()
    return configure_runtime(runtime, root), logger


class ToolCallItem:
    def __init__(self, call):
        self.tool_call = call
        self.meta = SimpleNamespace(logical_turn_id="offline-turn")


class ToolResult:
    """仅隔离宿主数据类导入，保持工具结果的文本与元数据协议。"""

    def __init__(self, tool_name="", success=True, content="", error_message="", metadata=None):
        self.tool_name, self.success = tool_name, success
        self.content, self.error_message = content, error_message
        self.metadata = metadata or {}

    def get_history_content(self):
        return self.content or self.error_message


def response(action="reply", *, text="完成", task="", call=None):
    return SimpleNamespace(
        response=json.dumps({"action": action, "track": "leave", "text": text, "task": task}),
        tool_calls=[call] if call else [],
        output_items=[ToolCallItem(call)] if call else [],
        model_name="offline",
        prompt_tokens=10,
        completion_tokens=10,
        prompt_cache_hit_tokens=0,
    )


def look():
    return SimpleNamespace(
        trigger_message_id="m1", requires_reply=True, no_quote_user_ids=frozenset(), reason="mention"
    )


def test_in_flight_tools_and_retry_keep_snapshot_and_next_turn_reloads(tmp_path):
    setup_files(tmp_path)
    runtime, logger = runtime_harness(tmp_path)
    light_requests, heavy_requests = [], []

    async def light(callback, options):
        light_requests.append(callback(None))
        return response("escalate", text="", task="查证原始问题")

    call = SimpleNamespace(func_name="web_search", args={"query": "原始问题"}, call_id="call1")
    heavy_results = [response(call=call), OSError("temporary failure"), response()]

    async def heavy(callback, options):
        heavy_requests.append(callback(None))
        value = heavy_results.pop(0)
        if isinstance(value, Exception):
            raise value
        return value

    async def invoke(*args, **kwargs):
        setup_files(tmp_path, version="1.1.0", step="仅后续任务的新指导")
        return SimpleNamespace(success=True, metadata={}, get_history_content=lambda: "实际结果")

    runtime._llm.generate_response_with_context.side_effect = light
    runtime._tool_llm.generate_response_with_context.side_effect = heavy
    runtime._invoke.side_effect = invoke
    asyncio.run(runtime._turn(look()))
    assert len(light_requests) == 1 and len(heavy_requests) == 3
    assert all("工作流" not in item.text for item in light_requests[0])
    guidance = [
        next(item.text for item in request if getattr(item, "role", None) == "system" and "本轮固定工作流" in item.text)
        for request in heavy_requests
    ]
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
    runtime._llm.generate_response_with_context.return_value = response(
        action, task="搜索" if action == "escalate" else ""
    )
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


@pytest.mark.parametrize("success", [True, False])
def test_teaching_reply_uses_actual_save_receipt_without_extra_model_call(tmp_path, success):
    runtime, _ = runtime_harness(tmp_path / "not-installed")
    runtime._llm.generate_response_with_context.return_value = response("escalate", task="总结教学")
    call = SimpleNamespace(func_name="learn_teaching", args={}, call_id="learn-one")
    runtime._tool_llm.generate_response_with_context.return_value = response(call=call)
    runtime._invoke.return_value = SimpleNamespace(
        success=success,
        metadata={"teaching_receipt": "我记住了：先交叉核实。\n关键词：核实"} if success else {},
        error_message="" if success else "fixture storage failed",
        get_history_content=lambda: "fixture",
    )
    asyncio.run(runtime._turn(look()))
    sent = runtime._send.await_args.args[0].text
    assert sent.startswith("我记住了：") if success else sent.startswith("这条教学暂未保存：")
    assert runtime._tool_llm.generate_response_with_context.await_count == 1


@pytest.mark.parametrize("requires_reply", [False, True])
def test_action_retry_preserves_current_reply_requirement(tmp_path, requires_reply):
    """格式补试不得把可沉默的请求变成强制回复。"""
    runtime, _ = runtime_harness(tmp_path)
    requests = []

    async def generate(callback, options):
        requests.append(callback(None))
        if len(requests) == 1:
            result = response()
            result.response = ""
            return result
        return response() if requires_reply else response("silent", text="")

    runtime._llm.generate_response_with_context.side_effect = generate
    validation = ValidationContext(
        requires_reply=requires_reply, quotable={}, no_quote_user_ids=frozenset(), max_chars=1500
    )
    _, action = asyncio.run(runtime._generate([], heavy=False, specs=[], validation=validation))
    correction = requests[1][-1].text
    assert action.action == ("reply" if requires_reply else "silent")
    if requires_reply:
        assert "本轮必须回应" in correction
    else:
        assert "本轮允许选择 silent" in correction
        assert "必须回应" not in correction


def test_empty_then_missing_track_can_submit_development_once(tmp_path):
    """复现空响应后的补试漏字段：开发仍提交一次，最终答复也只发一次。"""
    runtime, _ = runtime_harness(tmp_path / "not-installed")
    empty = response()
    empty.response = ""
    accepted = response("escalate", text="", task="实现图片反查并查证依赖安全性")
    accepted.response = json.dumps({"action": "escalate", "task": "实现图片反查并查证依赖安全性"})
    runtime._llm.generate_response_with_context.side_effect = [empty, accepted]
    call = SimpleNamespace(func_name="development_submit", args={}, call_id="dev-one")
    runtime._registry.list_tools.return_value.append(
        SimpleNamespace(name="development_submit", provider_type="builtin", to_llm_definition=lambda: {})
    )
    final = response(text="已排队：offline-dev-one")
    final.response = json.dumps({"action": "reply", "text": "已排队：offline-dev-one"})
    runtime._tool_llm.generate_response_with_context.side_effect = [response(call=call), final]
    runtime._invoke.return_value = ToolResult(content="开发任务已排队：offline-dev-one")

    asyncio.run(runtime._turn(look()))

    assert runtime._llm.generate_response_with_context.await_count == 2
    runtime._invoke.assert_awaited_once()
    assert runtime._invoke.await_args.args[0].func_name == "development_submit"
    runtime._send.assert_awaited_once()
    assert "offline-dev-one" in runtime._send.await_args.args[0].text
    assert runtime._send.await_args.args[0].track == "leave"


def real_tools(runtime, monkeypatch, clock=None):
    """使用生产工具执行方法和可推进的单调时钟，验证冷却但不实际等一分钟。"""
    runtime._invoke = type(runtime)._invoke.__get__(runtime)
    clock = clock if clock is not None else [1000.0]
    ns = type(runtime)._invoke.__globals__
    ns["time"] = SimpleNamespace(monotonic=lambda: clock[0], time=time.time)

    async def sleep(seconds):
        clock[0] += seconds

    ns["asyncio"].sleep.side_effect = sleep
    runtime._registry.invoke = AsyncMock(return_value=ToolResult(content="实际来源"))
    return ns["asyncio"].sleep


def test_rate_limit_waits_and_runs_each_search_once(tmp_path, monkeypatch):
    runtime, logger = runtime_harness(tmp_path / "not-installed")
    sleep = real_tools(runtime, monkeypatch)
    runtime._llm.generate_response_with_context.return_value = response("escalate", task="交叉检索")
    calls = [SimpleNamespace(func_name="web_search", args={"query": str(i)}, call_id=str(i)) for i in range(3)]
    batch = response(call=calls[0])
    batch.tool_calls = calls
    batch.output_items = [ToolCallItem(c) for c in calls]
    runtime._tool_llm.generate_response_with_context.side_effect = [batch, response(text="已查完三个来源")]
    asyncio.run(runtime._turn(look()))
    assert runtime._registry.invoke.await_count == 3
    assert [c.args[0].call_id for c in runtime._registry.invoke.await_args_list] == ["0", "1", "2"]
    sleep.assert_awaited_once()
    assert 60 <= sleep.await_args.args[0] <= 60.01
    assert runtime._tool_llm.generate_response_with_context.await_count == 2
    assert runtime._send.await_args.args[0].text == "已查完三个来源"


def test_cross_task_and_session_quota_waits_then_runs_only_new_search(tmp_path, monkeypatch):
    """前一任务或别的群占用同用户配额后，新任务等待并只执行新调用，不重放旧搜索。"""
    first, _ = runtime_harness(tmp_path / "not-installed")
    second = configure_runtime(type(first)(), tmp_path / "not-installed")
    assert first._tool_limiter is second._tool_limiter
    anchor = SimpleNamespace(message_info=SimpleNamespace(user_info=SimpleNamespace(user_id="1020050112")))
    first._anchor = second._anchor = lambda look: anchor
    first.session_id = "session-a"
    second.session_id = "session-b"
    second.chat_stream = SimpleNamespace(platform="qq", group_id="another-group")
    clock = [1000.0]
    sleep = real_tools(first, monkeypatch, clock=clock)
    real_tools(second, monkeypatch, clock=clock)

    first_calls = [
        SimpleNamespace(func_name="web_search", args={"query": "旧线索"}, call_id=f"a{index}") for index in range(2)
    ]
    first_batch = response(call=first_calls[0])
    first_batch.tool_calls, first_batch.output_items = first_calls, [ToolCallItem(call) for call in first_calls]
    first._llm.generate_response_with_context.return_value = response("escalate", task="先查线索")
    first._tool_llm.generate_response_with_context.side_effect = [first_batch, response(text="旧任务完成")]
    first._registry.invoke.return_value = ToolResult(content="1. 旧线索\n摘要\n来源：https://example.com/old")
    asyncio.run(first._turn(look()))
    assert [call.args[0].call_id for call in first._registry.invoke.await_args_list] == ["a0", "a1"]

    clock[0] = 1005.0
    sleep.reset_mock()
    second_calls = [
        SimpleNamespace(func_name="web_search", args={"query": "GitHub 项目"}, call_id="b0"),
        SimpleNamespace(func_name="read_webpage", args={"url": "https://github.com/example/project"}, call_id="b1"),
    ]
    second_batch = response(call=second_calls[0])
    second_batch.tool_calls, second_batch.output_items = second_calls, [ToolCallItem(call) for call in second_calls]
    requests = []

    async def generate(callback, options):
        requests.append(callback(None))
        return second_batch if len(requests) == 1 else response(text="新任务完成")

    second._llm.generate_response_with_context.return_value = response("escalate", task="复核 GitHub")
    second._tool_llm.generate_response_with_context.side_effect = generate
    second._registry.invoke.return_value = ToolResult(
        content="1. GitHub 项目\n摘要\n来源：https://github.com/example/project"
    )
    asyncio.run(second._turn(look()))

    # 旧任务的两条搜索没有被重放；新任务只执行自己的待执行调用。
    assert first._registry.invoke.await_count == 2
    assert [call.args[0].call_id for call in second._registry.invoke.await_args_list] == ["b0", "b1"]
    sleep.assert_awaited_once()
    assert 55 <= sleep.await_args.args[0] <= 55.02
    assert clock[0] == pytest.approx(1060.01)
    outputs = [item for item in requests[-1] if hasattr(item, "output")]
    assert [item.call_id for item in outputs] == ["b0", "b1"]
    assert second._send.await_args.args[0].text == "新任务完成"


def test_cancel_during_local_cooldown_does_not_execute_tool(tmp_path, monkeypatch):
    """本地冷却等待可被取消，不能继续执行工具或发送完成消息。"""
    runtime, _ = runtime_harness(tmp_path / "not-installed")
    clock = [1000.0]
    sleep = real_tools(runtime, monkeypatch, clock=clock)
    anchor = SimpleNamespace(message_info=SimpleNamespace(user_info=SimpleNamespace(user_id="1020050112")))
    assert runtime._tool_limiter.try_acquire("web_search:1020050112", 1000)
    assert runtime._tool_limiter.try_acquire("web_search:1020050112", 1000)
    clock[0] = 1005.0
    sleep.side_effect = asyncio.CancelledError()
    with pytest.raises(asyncio.CancelledError):
        asyncio.run(
            runtime._invoke(
                SimpleNamespace(func_name="web_search", args={"query": "GitHub"}, call_id="cancel"),
                [SimpleNamespace(name="web_search")],
                anchor,
                wait_budget=[120],
            )
        )
    runtime._registry.invoke.assert_not_awaited()
    runtime._send.assert_not_awaited()


def test_wait_resumes_same_context_and_does_not_drop_batch_tail(tmp_path, monkeypatch):
    runtime, _ = runtime_harness(tmp_path / "not-installed")
    sleep = real_tools(runtime, monkeypatch)
    runtime._registry.list_tools.return_value.append(
        SimpleNamespace(name="wait", provider_type="builtin", to_llm_definition=lambda: {})
    )
    runtime._llm.generate_response_with_context.return_value = response("escalate", task="稍等后查询")
    calls = [SimpleNamespace(func_name=name, args={}, call_id=str(i)) for i, name in enumerate(["wait", "web_search"])]
    batch = response(call=calls[0])
    batch.tool_calls, batch.output_items = calls, [ToolCallItem(c) for c in calls]
    requests = []

    async def generate(callback, options):
        requests.append(callback(None))
        return batch if len(requests) == 1 else response(text="查询完成")

    runtime._tool_llm.generate_response_with_context.side_effect = generate
    runtime._registry.invoke.side_effect = [
        ToolResult(metadata={"resume_after_seconds": 25}),
        ToolResult(content="已有来源"),
    ]
    asyncio.run(runtime._turn(look()))
    sleep.assert_awaited_once_with(25)
    assert runtime._registry.invoke.await_count == 2
    outputs = [item for item in requests[-1] if hasattr(item, "output")]
    assert [item.call_id for item in outputs] == ["0", "1"]
    assert "已等待 25 秒" in outputs[0].output
    assert outputs[1].output == "已有来源"
    runtime.gate.complete.assert_called_once()
    assert runtime._send.await_args.args[0].text == "查询完成"


def test_teaching_in_batch_does_not_discard_search(tmp_path):
    runtime, _ = runtime_harness(tmp_path / "not-installed")
    runtime._llm.generate_response_with_context.return_value = response("escalate", task="记住再检索")
    calls = [
        SimpleNamespace(func_name=name, args={}, call_id=str(i))
        for i, name in enumerate(["learn_teaching", "web_search"])
    ]
    batch = response(call=calls[0])
    batch.tool_calls, batch.output_items = calls, [ToolCallItem(c) for c in calls]
    runtime._invoke.side_effect = [
        ToolResult(content="已保存", metadata={"teaching_receipt": "我记住了"}),
        ToolResult(content="搜索结果"),
    ]
    runtime._tool_llm.generate_response_with_context.side_effect = [batch, response(text="记住并搜索完成")]
    asyncio.run(runtime._turn(look()))
    assert runtime._invoke.await_count == 2
    assert runtime._send.await_args.args[0].text == "记住并搜索完成"


@pytest.mark.parametrize("cause", ["wait_budget", "round_budget"])
def test_exhaustion_summarizes_results_with_tools_disabled(tmp_path, monkeypatch, cause):
    runtime, _ = runtime_harness(tmp_path / "not-installed")
    real_tools(runtime, monkeypatch)
    runtime._registry.list_tools.return_value.append(
        SimpleNamespace(name="wait", provider_type="builtin", to_llm_definition=lambda: {})
    )
    runtime._llm.generate_response_with_context.return_value = response("escalate", task="查询")
    name = "wait" if cause == "wait_budget" else "query_memory"
    runtime._registry.list_tools.return_value.append(
        SimpleNamespace(name=name, provider_type="builtin", to_llm_definition=lambda: {})
    )
    requests = []

    async def generate(callback, options):
        requests.append((callback(None), options))
        if not options["tool_options"]:
            return response(text="已有结果；其余尚未完成")
        return response(call=SimpleNamespace(func_name=name, args={}, call_id=str(len(requests))))

    runtime._tool_llm.generate_response_with_context.side_effect = generate
    runtime._registry.invoke.return_value = ToolResult(
        content="实际结果", metadata={"resume_after_seconds": 60} if name == "wait" else {}
    )
    asyncio.run(runtime._turn(look()))
    assert requests[-1][1]["tool_options"] == []
    assert runtime._registry.invoke.await_count == (
        runtime.config.task.max_wait_seconds // 60 if cause == "wait_budget" else runtime.config.task.max_tool_rounds
    )
    assert runtime._send.await_args.args[0].text == "已有结果；其余尚未完成"


def test_tool_exception_keeps_batch_outputs_and_cancel_propagates(tmp_path, monkeypatch):
    runtime, logger = runtime_harness(tmp_path / "not-installed")
    real_tools(runtime, monkeypatch)
    specs = [SimpleNamespace(name="web_search")]
    call = SimpleNamespace(func_name="web_search", args={}, call_id="one")
    runtime._registry.invoke.side_effect = ValueError("offline failure")
    result = asyncio.run(runtime._invoke(call, specs, None, wait_budget=[120]))
    assert not result.success and "ValueError" in result.error_message
    logger.exception.assert_called_once()
    runtime._registry.invoke.side_effect = asyncio.CancelledError()
    with pytest.raises(asyncio.CancelledError):
        asyncio.run(runtime._invoke(call, specs, None, wait_budget=[120]))


def test_cancel_during_wait_does_not_send_completion(tmp_path, monkeypatch):
    runtime, _ = runtime_harness(tmp_path / "not-installed")
    sleep = real_tools(runtime, monkeypatch)
    sleep.side_effect = asyncio.CancelledError()
    with pytest.raises(asyncio.CancelledError):
        asyncio.run(runtime._wait_for_task(25, [120], "model_wait"))
    runtime._send.assert_not_awaited()


def test_cooldown_budget_exhaustion_does_not_execute_tool(tmp_path, monkeypatch):
    runtime, logger = runtime_harness(tmp_path / "not-installed")
    sleep = real_tools(runtime, monkeypatch)
    runtime._tool_limiter.try_acquire("web_search:", 1000)
    runtime._tool_limiter.try_acquire("web_search:", 1000)
    budget = [5]
    result = asyncio.run(
        runtime._invoke(
            SimpleNamespace(func_name="web_search", args={}, call_id="blocked"),
            [SimpleNamespace(name="web_search")],
            None,
            wait_budget=budget,
        )
    )
    assert not result.success and "尚未执行" in result.error_message
    assert budget == [0]
    sleep.assert_not_awaited()
    runtime._registry.invoke.assert_not_awaited()
    logger.warning.assert_called_once()


def test_insufficient_wait_budget_keeps_sources_and_names_unexecuted_review(tmp_path, monkeypatch):
    """本地冷却等待预算不够时，保留已成功来源并点名尚未执行的 GitHub 复核。"""
    runtime, _ = runtime_harness(tmp_path / "not-installed")
    clock = [1000.0]
    sleep = real_tools(runtime, monkeypatch, clock=clock)
    anchor = SimpleNamespace(message_info=SimpleNamespace(user_info=SimpleNamespace(user_id="1020050112")))
    runtime._anchor = lambda look: anchor

    # 前一任务或别的群已经用掉该用户两次 read_webpage 配额。
    old_calls = [
        SimpleNamespace(func_name="read_webpage", args={"url": f"https://example.com/{index}"}, call_id=f"a{index}")
        for index in range(2)
    ]
    old_batch = response(call=old_calls[0])
    old_batch.tool_calls, old_batch.output_items = old_calls, [ToolCallItem(call) for call in old_calls]
    runtime._llm.generate_response_with_context.return_value = response("escalate", task="旧任务")
    runtime._tool_llm.generate_response_with_context.side_effect = [old_batch, response(text="旧任务完成")]
    runtime._registry.invoke.return_value = ToolResult(content="旧页面\n来源：https://example.com/old")
    asyncio.run(runtime._turn(look()))

    runtime.config = replace(runtime.config, task=replace(runtime.config.task, max_wait_seconds=5))
    clock[0] = 1005.0
    sleep.reset_mock()
    runtime._send.reset_mock()
    runtime._registry.invoke.reset_mock()
    runtime._tool_llm.generate_response_with_context.reset_mock()
    calls = [
        SimpleNamespace(func_name="web_search", args={"query": "GitHub 项目"}, call_id="s"),
        SimpleNamespace(
            func_name="read_webpage", args={"url": "https://github.com/example/project/issues/1"}, call_id="r"
        ),
    ]
    batch = response(call=calls[0])
    batch.tool_calls, batch.output_items = calls, [ToolCallItem(call) for call in calls]
    runtime._tool_llm.generate_response_with_context.side_effect = [batch, response(text="不应走模型收尾")]
    runtime._registry.invoke.side_effect = [
        ToolResult(content="1. GitHub 项目\n项目说明\n来源：https://github.com/example/project"),
        ToolResult(content="不应执行"),
    ]
    asyncio.run(runtime._turn(look()))

    assert runtime._registry.invoke.await_count == 1
    assert runtime._registry.invoke.await_args.args[0].call_id == "s"
    sent = runtime._send.await_args.args[0].text
    assert "https://github.com/example/project" in sent
    assert "read_webpage" in sent and "https://github.com/example/project/issues/1" in sent
    assert "本地冷却" in sent and "尚未执行" in sent
    assert "暂未取得可用资料" not in sent
    assert "操作过于频繁" not in sent
    assert len(sent) <= runtime.config.reply.max_chars
    # 预算已耗尽时由宿主直接交付阶段结果，不再请求模型猜测是否限流。
    assert runtime._tool_llm.generate_response_with_context.await_count == 1


@pytest.mark.parametrize("delivery_started", [False, True])
def test_turn_failure_reports_once_without_retrying_uncertain_delivery(tmp_path, delivery_started):
    runtime, logger = runtime_harness(tmp_path / "not-installed")
    runtime._running = True
    runtime._event = SimpleNamespace(clear=Mock())
    runtime._breaker = SimpleNamespace(allow=lambda _: True, record_failure=Mock())
    runtime.gate = SimpleNamespace(step=lambda _: look(), in_flight=look(), abort=Mock())

    async def fail(_):
        runtime._response_started = delivery_started
        runtime._running = False
        raise RuntimeError("offline failure")

    runtime._turn = fail
    asyncio.run(runtime._run())
    assert runtime._send.await_count == (0 if delivery_started else 1)
    runtime._after_turn.assert_awaited_once()
    runtime.gate.abort.assert_called_once_with(retry=False)


def test_cleanup_hook_failure_does_not_kill_session(tmp_path):
    runtime, logger = runtime_harness(tmp_path / "not-installed")
    ns = type(runtime)._after_turn.__globals__
    ns["get_plugin_runtime_manager"] = lambda: SimpleNamespace(
        invoke_hook=AsyncMock(side_effect=ValueError("hook failed"))
    )
    asyncio.run(type(runtime)._after_turn(runtime, look(), False))
    logger.exception.assert_called_once()


@pytest.mark.parametrize(
    "seconds,success", [(25, True), (0, True), (61, False), (-1, False), ("25", False), (True, False)]
)
def test_builtin_wait_returns_resumable_result(seconds, success):
    """直接执行生产 wait 提供器，确认工具协议不再终止本轮。"""
    path = Path(__file__).resolve().parents[2] / "src/kkbk_engine/builtin_tools.py"
    klass = next(node for node in ast.parse(path.read_text(encoding="utf-8")).body if isinstance(node, ast.ClassDef))
    ns = {"Any": object, "ToolExecutionResult": ToolResult}
    exec(compile(ast.Module(body=[klass], type_ignores=[]), str(path), "exec"), ns)
    provider = ns["KKBKBuiltinToolProvider"].__new__(ns["KKBKBuiltinToolProvider"])
    result = asyncio.run(provider.invoke(SimpleNamespace(tool_name="wait", arguments={"seconds": seconds})))
    assert result.success == success
    assert "pause_execution" not in result.metadata
    if success:
        assert result.metadata["resume_after_seconds"] == seconds


@pytest.mark.parametrize("success", [True, False])
def test_search_budget_exhaustion_delivers_actual_evidence_without_lossy_model_summary(tmp_path, success):
    runtime, _ = runtime_harness(tmp_path / "not-installed")
    runtime._llm.generate_response_with_context.return_value = response("escalate", task="查清事件")
    call = SimpleNamespace(func_name="web_search", args={}, call_id="search")
    runtime._tool_llm.generate_response_with_context.return_value = response(call=call)

    async def search(*args, wait_budget):
        wait_budget[0] = 0
        return ToolResult(
            success=success,
            content=(
                "1. 官方仓库\n已取得项目说明，但事件原因还未核实。\n来源：https://github.com/example/project"
                if success
                else "未取得结果"
            ),
        )

    runtime._invoke.side_effect = search
    asyncio.run(runtime._turn(look()))
    delivered = runtime._send.await_args.args[0].text
    if success:
        assert "官方仓库" in delivered and "https://github.com/example/project" in delivered
        assert "阶段结果" in delivered and "尚未完成" in delivered
    else:
        assert "未取得可用来源" in delivered and "工具返回失败" in delivered
        assert "未取得结果" in delivered and "官方仓库" not in delivered
        assert "暂未取得可用资料" not in delivered
    # 预算耗尽后不再请求模型，避免再次把已有来源说成一条都没搜到。
    assert runtime._tool_llm.generate_response_with_context.await_count == 1
    assert len(delivered) <= runtime.config.reply.max_chars


def test_upstream_failure_is_reported_as_failure_and_not_retried(tmp_path):
    """远端失败只交付一次并保留原因，不能改写成“没有结果”。"""
    runtime, _ = runtime_harness(tmp_path / "not-installed")
    runtime._invoke = type(runtime)._invoke.__get__(runtime)
    production_invoke = runtime._invoke

    async def invoke(*args, wait_budget, **kwargs):
        result = await production_invoke(*args, wait_budget=wait_budget, **kwargs)
        # 强制进入宿主交付阶段，验证失败原因不会在收尾时丢失。
        wait_budget[0] = 0
        return result

    runtime._invoke = invoke
    runtime._llm.generate_response_with_context.return_value = response("escalate", task="查 GitHub")
    call = SimpleNamespace(func_name="web_search", args={"query": "GitHub 项目"}, call_id="failed")
    runtime._tool_llm.generate_response_with_context.return_value = response(call=call)
    runtime._registry.invoke = AsyncMock(return_value=ToolResult(success=False, error_message="HTTP 403"))
    asyncio.run(runtime._turn(look()))
    delivered = runtime._send.await_args.args[0].text
    assert runtime._registry.invoke.await_count == 1
    assert "未取得可用来源" in delivered
    assert "工具返回失败" in delivered and "HTTP 403" in delivered
    assert "暂未取得可用资料" not in delivered

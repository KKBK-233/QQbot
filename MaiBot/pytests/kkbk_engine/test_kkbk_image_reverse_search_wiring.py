"""证明搜图工具已接入 KKBK agent，并能把消息定位信息交给插件工具。"""

from pathlib import Path
from types import SimpleNamespace
from typing import Any, Dict, Optional

import ast

ROOT = Path(__file__).resolve().parents[2]


def test_image_reverse_search_is_agent_plugin_tool() -> None:
    from src.kkbk_engine.config import EngineConfig

    assert "image_reverse_search" in EngineConfig().plugin_tools


def test_message_locators_are_reserved_from_model_args() -> None:
    source = (ROOT / "src/kkbk_engine/tool_execution.py").read_text(encoding="utf-8")
    assert '"anchor_message_id"' in source
    assert '"reply_to_message_id"' in source


def test_system_prompt_tells_model_to_use_image_tool_without_url() -> None:
    tree = ast.parse((ROOT / "src/kkbk_engine/prompt.py").read_text(encoding="utf-8"))
    assignment = next(
        node
        for node in tree.body
        if isinstance(node, ast.Assign)
        and any(isinstance(target, ast.Name) and target.id == "RULES_TEXT" for target in node.targets)
    )
    rules = ast.literal_eval(assignment.value)
    assert "image_reverse_search" in rules
    assert "不要向用户索要图片 URL 或 Base64" in rules


def test_tool_context_carries_anchor_and_quoted_message_ids() -> None:
    path = ROOT / "src/plugin_runtime/component_query.py"
    tree = ast.parse(path.read_text(encoding="utf-8"))
    class_node = next(
        node for node in tree.body if isinstance(node, ast.ClassDef) and node.name == "ComponentQueryService"
    )
    function = next(
        node
        for node in class_node.body
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name == "_build_tool_context_payload"
    )
    module = ast.Module(body=[function], type_ignores=[])
    namespace = {
        "Any": Any,
        "Dict": Dict,
        "Optional": Optional,
        "ToolExecutionContext": object,
    }
    exec(compile(module, str(path), "exec"), namespace)

    anchor = SimpleNamespace(
        message_id="m-current",
        reply_to="m-quoted",
        message_info=SimpleNamespace(
            group_info=SimpleNamespace(group_id="g1"),
            user_info=SimpleNamespace(user_id="u1"),
        ),
    )
    context = SimpleNamespace(
        stream_id="s1",
        session_id="s1",
        group_id="",
        user_id="",
        platform="qq",
        metadata={"anchor_message": anchor},
    )

    payload = namespace["_build_tool_context_payload"](context)

    assert payload["anchor_message_id"] == "m-current"
    assert payload["reply_to_message_id"] == "m-quoted"


def test_tool_context_marks_plugin_declaration() -> None:
    source = (ROOT / "src/plugin_runtime/component_query.py").read_text(encoding="utf-8")
    assert 'for key in ("anchor_message_id", "reply_to_message_id"):' in source
    assert "payload[key] = context_payload[key]" in source


def test_command_payload_can_include_original_image_bytes() -> None:
    source = (ROOT / "src/plugin_runtime/component_query.py").read_text(encoding="utf-8")
    assert 'metadata.get("include_binary_data", False)' in source

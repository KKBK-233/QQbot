"""新引擎的插件接入点，沿用宿主公开的 Context Item 协议。"""

from src.plugin_runtime.host.hook_spec_registry import HookSpec, HookSpecRegistry


def register_kkbk_hook_specs(registry: HookSpecRegistry) -> list[HookSpec]:
    """请求前注入当前任务和媒体事实，结束后清理插件的短期任务状态。"""
    properties = {
        "session_id": {"type": "string"},
        "trigger_message_id": {"type": "string"},
        "requires_reply": {"type": "boolean"},
        "items": {"type": "array"},
        "tool_definitions": {"type": "array"},
        "success": {"type": "boolean"},
    }
    return registry.register_hook_specs([
        HookSpec(name="kkbk.before_request", description="新引擎请求前的任务及媒体事实", 
                 parameters_schema={"type": "object", "properties": properties}, default_timeout_ms=6000),
        HookSpec(name="kkbk.after_turn", description="新引擎完成当前任务后的状态清理",
                 parameters_schema={"type": "object", "properties": properties}, default_timeout_ms=1000),
    ])

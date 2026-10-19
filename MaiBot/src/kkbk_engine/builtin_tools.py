"""复用已有记忆服务，提供新引擎实际支持的内置工具。"""

from typing import Any

import time

from src.core.tooling import ToolExecutionResult
from src.maisaka.builtin_tool import BUILTIN_TOOL_ENTRIES
from src.maisaka.builtin_tool.context import BuiltinToolRuntimeContext

MEMORY_TOOLS = frozenset({
    "remember_memory", "query_memory", "query_person_profile", "list_known_people", "query_image_memory",
})


class KKBKBuiltinToolProvider:
    """记忆处理器继续调用原服务，wait 直接控制新引擎的定时器。"""

    provider_name = "kkbk_builtin"
    provider_type = "builtin"

    def __init__(self, runtime: Any) -> None:
        self.runtime = runtime
        self.context = BuiltinToolRuntimeContext(engine=runtime, runtime=runtime)
        self.entries = {entry.name: entry for entry in BUILTIN_TOOL_ENTRIES
                        if entry.name in MEMORY_TOOLS | {"wait"}}

    async def list_tools(self, context=None):
        """沿用原记忆开关，不暴露依赖旧规划器的 reply、switch_chat 等工具。"""
        return [entry.build_spec() for entry in self.entries.values()]

    async def invoke(self, invocation, context=None):
        """执行记忆工具，等待操作只暂停主动追踪，新的点名仍可唤醒。"""
        if invocation.tool_name == "wait":
            seconds = max(0, int(invocation.arguments.get("seconds", 30)))
            self.runtime.wait_until = time.time() + seconds
            return ToolExecutionResult(tool_name="wait", success=True, content=f"等待 {seconds} 秒。",
                                       metadata={"pause_execution": True})
        return await self.entries[invocation.tool_name].handle_tool(self.context, invocation, context)

    async def close(self):
        """记忆服务由宿主管理，不随单群运行时关闭。"""

"""复用已有记忆服务，提供新引擎实际支持的内置工具。"""

from typing import Any

from src.core.tooling import ToolExecutionResult
from src.maisaka.builtin_tool import BUILTIN_TOOL_ENTRIES
from src.maisaka.builtin_tool.context import BuiltinToolRuntimeContext

MEMORY_TOOLS = frozenset({
    "remember_memory", "query_memory", "query_person_profile", "list_known_people", "query_image_memory",
})


class KKBKBuiltinToolProvider:
    """记忆处理器继续调用原服务，wait 保留本轮任务并定时续跑。"""

    provider_name = "kkbk_builtin"
    provider_type = "builtin"

    def __init__(self, runtime: Any) -> None:
        self.runtime = runtime
        self.context = BuiltinToolRuntimeContext(engine=runtime, runtime=runtime)
        self.entries = {entry.name: entry for entry in BUILTIN_TOOL_ENTRIES
                        if entry.name in MEMORY_TOOLS | {"wait"}}

    async def list_tools(self, context=None):
        """沿用原记忆开关，不暴露依赖旧规划器的 reply、switch_chat 等工具。"""
        specs = [entry.build_spec() for entry in self.entries.values()]
        for spec in specs:
            if spec.name == "wait":
                spec.description = "等待指定秒数后自动继续当前任务，保留已有工具结果；不需要用户再次唤醒。单次最多 60 秒，本轮累计等待最多 120 秒。"
                spec.parameters_schema = {"type": "object", "properties": {
                    "seconds": {"type": "integer", "minimum": 0, "maximum": 60}},
                    "required": ["seconds"]}
        return specs

    async def invoke(self, invocation, context=None):
        """执行记忆工具；等待由宿主在同一工具上下文中完成。"""
        if invocation.tool_name == "wait":
            seconds = invocation.arguments.get("seconds", 30)
            if type(seconds) is not int or not 0 <= seconds <= 60:
                return ToolExecutionResult(tool_name="wait", success=False,
                                           error_message="等待秒数必须是 0 到 60 的整数")
            return ToolExecutionResult(tool_name="wait", success=True, content=f"请求等待 {seconds} 秒后继续。",
                                       metadata={"resume_after_seconds": seconds})
        return await self.entries[invocation.tool_name].handle_tool(self.context, invocation, context)

    async def close(self):
        """记忆服务由宿主管理，不随单群运行时关闭。"""

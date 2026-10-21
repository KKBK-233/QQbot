"""工具执行与有界等待，保留宿主身份、频率限制及结果不重放语义。"""

import asyncio
import json
import time

from src.common.logger import get_logger
from src.core.tooling import ToolInvocation, ToolExecutionContext
from .guards import ConcurrencyGate, QueueFullError, QueueTimeoutError

logger = get_logger("kkbk_engine")


class ToolExecutionMixin:
    """只负责工具执行，不组装模型请求、不轮询开发通知。"""

    async def _wait_for_task(self, seconds: float, budget: list, reason: str) -> bool:
        """有界异步等待，取消由 asyncio 正常向上传播，不结束当前任务。"""
        if not isinstance(seconds, (int, float)) or not 0 <= seconds <= 60.01:
            return False
        if seconds > budget[0]:
            budget[0] = 0.0
            return False
        budget[0] -= seconds
        logger.info(
            json.dumps(
                {"event": "task_wait", "session_id": self.session_id, "seconds": seconds, "reason": reason},
                ensure_ascii=False,
            )
        )
        await asyncio.sleep(seconds)
        return True

    async def _invoke(self, call, specs, anchor, *, wait_budget):
        """在宿主绑定真实会话和发送者，沿用插件权限、限流和沙箱资源边界。"""
        from src.core.tooling import ToolExecutionResult

        if call.func_name not in {spec.name for spec in specs}:
            return ToolExecutionResult(tool_name=call.func_name, success=False, error_message="工具未开放")
        user_id = str(anchor.message_info.user_info.user_id) if anchor else ""
        if call.func_name in self.config.plugin_tools:
            key = f"{call.func_name}:{user_id}"
            while not self._tool_limiter.try_acquire(key, time.monotonic()):
                delay = self._tool_limiter.retry_after(key, time.monotonic()) + 0.01
                if not await self._wait_for_task(delay, wait_budget, "tool_rate_limit:" + call.func_name):
                    # 只说明宿主本地冷却和未执行，不能把等待超时归因为远端站点限流。
                    result = ToolExecutionResult(
                        tool_name=call.func_name,
                        success=False,
                        error_message=(
                            "宿主本地冷却：同用户近期已使用该工具名额，等待预算不足，"
                            "本次调用尚未执行且未向远端发出请求；不能据此判断远端站点限流。"
                        ),
                        metadata={"kkbk_not_executed": "local_rate_limit"},
                    )
                    logger.warning(
                        json.dumps(
                            {
                                "event": "tool",
                                "session_id": self.session_id,
                                "tool": call.func_name,
                                "success": False,
                                "result": result.error_message,
                            },
                            ensure_ascii=False,
                        )
                    )
                    return result
        # 工具参数中的会话或身份不能覆盖宿主的真实来源。
        # 消息定位由宿主按触发消息注入，模型不能指定其他消息的原图。
        reserved = {
            "stream_id",
            "chat_id",
            "session_id",
            "user_id",
            "group_id",
            "platform",
            "anchor_message_id",
            "reply_to_message_id",
            "_sandbox_trusted_context",
        }
        args = {key: value for key, value in call.args.items() if key not in reserved}
        invocation = ToolInvocation(
            tool_name=call.func_name,
            arguments=args,
            call_id=call.call_id,
            session_id=self.session_id,
            stream_id=self.session_id,
        )
        context = ToolExecutionContext(
            session_id=self.session_id,
            stream_id=self.session_id,
            platform=self.chat_stream.platform,
            is_group_chat=True,
            group_id=self.chat_stream.group_id or "",
            user_id=user_id,
            metadata={"anchor_message": anchor},
        )
        gate = self._tool_gates.setdefault(call.func_name, ConcurrencyGate(2, 4, 15.0))
        try:
            async with gate.slot():
                result = await self._registry.invoke(invocation, context)
        except (QueueFullError, QueueTimeoutError):
            result = ToolExecutionResult(
                tool_name=call.func_name, success=False, error_message="工具繁忙或排队超时，请稍后再试"
            )
        except Exception as exc:
            # 单个工具失败仍返回对应 call_id 的结果，不吞掉同批剩余调用；不重试有副作用的工具。
            logger.exception(f"{self.log_prefix} 工具 {call.func_name} 执行异常")
            result = ToolExecutionResult(
                tool_name=call.func_name,
                success=False,
                error_message=f"工具执行异常（{type(exc).__name__}），本次结果未确认，请勿声称完成。",
            )
        if "resume_after_seconds" in result.metadata:
            seconds = result.metadata["resume_after_seconds"]
            if await self._wait_for_task(seconds, wait_budget, "model_wait"):
                result = ToolExecutionResult(
                    tool_name=call.func_name, success=True, content=f"已等待 {seconds} 秒，请继续当前任务。"
                )
            else:
                result = ToolExecutionResult(
                    tool_name=call.func_name,
                    success=False,
                    error_message="本轮等待预算已用完，请汇总已有结果及未完成部分。",
                )
        elif result.metadata.get("pause_execution"):
            # 未接入续跑协议的旧工具不能把整轮静默结束。
            result = ToolExecutionResult(
                tool_name=call.func_name,
                success=False,
                error_message="该工具请求暂停，但未提供续跑时间；请报告已完成部分及该阻塞，不要承诺自动继续。",
            )
        logger.info(
            json.dumps(
                {
                    "event": "tool",
                    "session_id": self.session_id,
                    "tool": call.func_name,
                    "success": result.success,
                    "result": result.get_history_content(),
                },
                ensure_ascii=False,
            )
        )
        return result

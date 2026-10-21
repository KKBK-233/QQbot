"""请求上下文组装：稳定系统前缀、本轮工具清单及任务阶段教学。"""

from html import escape
import time

from src.config.config import global_config
from src.common.logger import get_logger
from src.llm_models.payload_content.context_item import RoleType
from src.plugin_runtime.hook_payloads import serialize_prompt_items, serialize_tool_definitions
from src.plugin_runtime.integration import get_plugin_runtime_manager

from .context_items import text_item
from .prompt import build_system_prompt, build_messages, render_turn_block

logger = get_logger("kkbk_engine")


class RequestContextMixin:
    """与回合编排分离；查询相关资料只在进入工具阶段加载。"""

    async def _prepare(self, look, specs):
        """插件只读取当前快照；新增的事实放在历史末尾，避免旧任务污染新请求。"""
        persona = global_config.personality
        system = build_system_prompt(
            "\n\n".join(
                [
                    persona.personality,
                    persona.behavior_style,
                    persona.reply_style,
                    global_config.chat.reply_style.group_chat_prompt,
                ]
            ),
            self.config.identity,
            {},
            "",
        )
        hook_items = []
        for message in list(self.messages.values()):
            user = message.message_info.user_info
            is_self = str(user.user_id) == self.config.identity.bot_user_id
            header = (
                f'<message msg_id="{escape(str(message.message_id), quote=True)}" '
                f'is_self_message="{str(is_self).lower()}">'
            )
            hook_items.append(text_item(RoleType.User, header + "\n" + (message.processed_plain_text or "")))
        serialized = serialize_prompt_items(hook_items)
        result = await get_plugin_runtime_manager().invoke_hook(
            "kkbk.before_request",
            session_id=self.session_id,
            trigger_message_id=look.trigger_message_id or "",
            requires_reply=look.requires_reply,
            items=serialized,
            tool_definitions=serialize_tool_definitions([spec.to_llm_definition() for spec in specs]),
        )
        # 插件保留旧资料读取方式，只接收在消息快照之后追加的本轮事实。
        extra = result.kwargs.get("items", serialized)[len(serialized) :]
        # 本轮权限相关清单放在历史末尾，维护者与普通群友共享稳定系统前缀。
        facts = ["本轮可用工具：" + "、".join(sorted(spec.name for spec in specs))]
        for item in extra:
            facts.extend(str(part.get("text", "")) for part in item.get("parts", []) if part.get("type") == "text")
        turn = render_turn_block(look, time.time(), facts)
        messages = build_messages(system, self.history.render(), turn)
        return [
            text_item(RoleType.System if msg["role"] == "system" else RoleType.User, msg["content"]) for msg in messages
        ]

    async def _teaching_guidance(self, look):
        """只有工具任务召回教学；普通聊天不查库、不携带动态教学正文。"""
        anchor = self._anchor(look)
        query = (anchor.processed_plain_text or "") if anchor else ""
        try:
            return await self._development.teaching.guidance(query)
        except Exception as exc:
            logger.warning(f"{self.log_prefix} 教学记忆读取失败：{type(exc).__name__}: {exc}")
            return "【教学读取状态】教学记忆读取失败，本轮没有取得教学资料，不代表此前没有保存。"

"""引擎内部使用的入站消息结构。

门控和 prompt 组装只认这个结构，不直接依赖 SessionMessage，便于用导出的聊天记录离线回放。
segments 采用 MessageSequence.to_dict() 的字典格式。
"""

from dataclasses import dataclass
from typing import Any, Dict, Mapping, Optional, Tuple


@dataclass(frozen=True)
class InboundMessage:
    """一条已入库的群消息。"""

    message_id: str
    timestamp: float
    user_id: str
    nickname: str
    cardname: str = ""
    text: str = ""
    """顶层原始文本组件拼接，用于点名和问题判断。"""
    visible_text: str = ""
    """MaiBot 处理后的可读文本，没有 segments 时给模型看。"""
    mentions: Tuple[str, ...] = ()
    """结构化 @ 的目标 QQ 号。"""
    reply_to: Optional[str] = None
    reply_sender_id: Optional[str] = None
    is_notify: bool = False
    is_command: bool = False
    segments: Tuple[Dict[str, Any], ...] = ()

    @property
    def display_name(self) -> str:
        """群名片优先，其次昵称，最后 QQ 号。"""
        return self.cardname or self.nickname or self.user_id


def from_export_row(row: Mapping[str, Any]) -> InboundMessage:
    """从 chat-exports 的 messages.jsonl 行构造消息。"""
    return InboundMessage(
        message_id=str(row["message_id"]),
        timestamp=float(row["epoch_seconds"]),
        user_id=str(row["user_id"]),
        nickname=row["nickname"] or "",
        cardname=row["cardname"] or "",
        text=row["raw_text"] or "",
        visible_text=row["processed_text"] or "",
        mentions=tuple(str(user_id) for user_id in row["mentions"] or ()),
        reply_to=row["reply_to"] or None,
        is_notify=bool(row["is_notify"]),
        is_command=bool(row["is_command"]),
        segments=tuple(row["segments"] or ()),
    )

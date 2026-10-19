"""点名与问题判断，纯代码，不调模型。"""

from typing import Callable, Optional

import re

from .config import IdentityConfig
from .inbound import InboundMessage

QUESTION_RE = re.compile(
    r"[？?]|吗[。！!～~]*$|呢[。！!～~]*$|什么|怎么|为什么|为啥|咋|有没有|是不是|能不能|会不会|谁|哪"
)

# SnowLuma 适配器渲染群戳一戳通知时固定追加的说明；通知里不保留结构化 target_id，只能按文本解析。
POKE_SUFFIX = "（这是QQ的一个功能，用于提及某人，但没那么明显）"

MENTION_AT = "at"
MENTION_QUOTE_BOT = "quote_bot"
MENTION_NAME = "name"
MENTION_POKE = "poke"


class MentionDetector:
    """判断一条消息是否在点名 KKBK。"""

    def __init__(self, identity: IdentityConfig) -> None:
        self._bot_user_id = identity.bot_user_id
        self._wake_re = re.compile("|".join(re.escape(word) for word in identity.wake_words), re.IGNORECASE)

    def detect(self, message: InboundMessage, is_bot_message: Callable[[str], bool]) -> Optional[str]:
        """返回点名类型，不是点名返回 None。

        Args:
            message: 待判断的消息。
            is_bot_message: 判断某个消息 ID 是否为 KKBK 自己发出。
        """
        if message.is_notify:
            return MENTION_POKE if self._is_poke_on_bot(message) else None
        if self._bot_user_id in message.mentions:
            return MENTION_AT
        if message.reply_to and (message.reply_sender_id == self._bot_user_id or is_bot_message(message.reply_to)):
            return MENTION_QUOTE_BOT
        if self._wake_re.search(message.text):
            return MENTION_NAME
        return None

    def _is_poke_on_bot(self, message: InboundMessage) -> bool:
        """通知文本形如“{发起人}{动作}{目标}{后缀}”，去掉发起人名字后在剩余部分找唤醒名。"""
        text = message.text
        if not text.endswith(POKE_SUFFIX):
            return False
        body = text[: -len(POKE_SUFFIX)]
        actor_names = sorted({message.cardname, message.nickname, message.user_id} - {""}, key=len, reverse=True)
        for name in actor_names:
            if body.startswith(name):
                return self._wake_re.search(body[len(name) :]) is not None
        # 发起人名字对不上就无法区分谁戳谁，不当作点名
        return False


def is_question(message: InboundMessage, *, min_chars: int) -> bool:
    """没有 @、没有引用的普通提问。"""
    if message.is_notify or message.mentions or message.reply_to:
        return False
    text = message.text
    return len(text) >= min_chars and QUESTION_RE.search(text) is not None

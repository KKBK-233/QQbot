"""模型动作的解析与宿主校验。

模型每次只输出一个 JSON 动作；即使被注入，最坏结果也只是一个被拒绝的动作。
校验失败抛出 ActionError，由调用方决定是否带着错误码重问一次，不在这里静默修正。
"""

from dataclasses import dataclass
from typing import FrozenSet, Mapping, Optional

import json
import re

ACTIONS = frozenset({"silent", "reply", "escalate"})
TRACKS = frozenset({"stay", "leave"})
ALLOWED_KEYS = frozenset({"action", "text", "quote", "task", "track", "reason"})

_FENCE_RE = re.compile(r"^```(?:json)?\s*(.*?)\s*```$", re.DOTALL)
_AT_ALL_RE = re.compile(r"@\s*(?:全体成员|全体|所有人)|@\s*(?:everyone|all)\b|\[CQ:at,qq=all\]", re.IGNORECASE)


class ActionError(Exception):
    """动作被拒绝。code 稳定，可写进影子日志或作为重问提示。"""

    def __init__(self, code: str, detail: str = "") -> None:
        super().__init__(f"{code}: {detail}" if detail else code)
        self.code = code
        self.detail = detail


@dataclass(frozen=True)
class ModelAction:
    action: str
    track: str
    text: str = ""
    quote: Optional[str] = None
    task: str = ""
    reason: str = ""


@dataclass(frozen=True)
class ValidationContext:
    requires_reply: bool
    """点名时不允许 silent（防死循环冷却期的非 @ 点名不会走到这里）。"""
    quotable: Mapping[str, str]
    """当前上下文里可引用的消息编号 -> 发送者 QQ 号。"""
    no_quote_user_ids: FrozenSet[str]
    max_chars: int


def parse_action(raw: str) -> ModelAction:
    """严格解析模型输出；允许外面包一层 ```json 代码块。"""
    text = raw.strip()
    if fence := _FENCE_RE.match(text):
        text = fence.group(1)
    try:
        data = json.loads(text)
    except json.JSONDecodeError as exc:
        raise ActionError("invalid_json", str(exc)) from exc
    if not isinstance(data, dict):
        raise ActionError("not_object", type(data).__name__)
    unknown_keys = set(data) - ALLOWED_KEYS
    if unknown_keys:
        raise ActionError("unknown_keys", ",".join(sorted(unknown_keys)))
    for key, value in data.items():
        if value is not None and not isinstance(value, str):
            raise ActionError("bad_type", key)

    action = data.get("action")
    if action not in ACTIONS:
        raise ActionError("bad_action", str(action))
    track = data.get("track")
    if track not in TRACKS:
        raise ActionError("bad_track", str(track))
    return ModelAction(
        action=action,
        track=track,
        text=(data.get("text") or "").strip(),
        quote=(data.get("quote") or "").strip() or None,
        task=(data.get("task") or "").strip(),
        reason=(data.get("reason") or "").strip(),
    )


def validate_action(action: ModelAction, context: ValidationContext) -> ModelAction:
    """按宿主规则校验动作，通过则原样返回。"""
    if action.action == "silent":
        if context.requires_reply:
            raise ActionError("silent_on_mention")
        if action.text or action.quote or action.task:
            raise ActionError("silent_with_payload")
        return action

    if action.action == "reply":
        if not action.text:
            raise ActionError("empty_reply")
        if action.task:
            raise ActionError("reply_with_task")
    elif not action.task:
        raise ActionError("empty_task")

    # escalate 可带一句先行回应，与 reply 正文同样校验
    if action.text:
        if len(action.text) > context.max_chars:
            raise ActionError("too_long", f"{len(action.text)}>{context.max_chars}")
        if _AT_ALL_RE.search(action.text):
            raise ActionError("at_all")
    if action.quote is not None:
        quoted_user_id = context.quotable.get(action.quote)
        if quoted_user_id is None:
            raise ActionError("unknown_quote", action.quote)
        if quoted_user_id in context.no_quote_user_ids:
            raise ActionError("quote_forbidden", action.quote)
    return action

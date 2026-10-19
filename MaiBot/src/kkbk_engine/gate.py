"""会话级门控状态机。

代码决定何时让模型看一眼，模型决定看到后做什么。一个会话只有两个状态：
- 静态扫描：只跑代码；点名、问题计时（60 秒没人接）、3% 随机探索命中才调用模型；
- 追踪：每批消息结束（3 秒防抖）调用一次模型；180 秒无新理由、模型连续表示离开或连续空转后退出。

每个会话同时只有一个请求在飞，期间到达的消息并入下一批。
状态机不读时钟，所有时间由调用方传入，便于离线回放和测试。
"""

from collections import OrderedDict, deque
from dataclasses import dataclass
from typing import Deque, FrozenSet, Iterable, List, Optional, Tuple

import random

from .config import EngineConfig
from .guards import LoopBreaker
from .inbound import InboundMessage
from .triggers import MENTION_AT, MentionDetector, is_question

STATE_SCAN = "scan"
STATE_TRACK = "track"

REASON_MENTION = "mention"
REASON_QUESTION = "question"
REASON_TRACK = "track"
REASON_EXPLORE = "explore"

ACTION_SILENT = "silent"
TRACK_STAY = "stay"
TRACK_LEAVE = "leave"

MAX_REMEMBERED_BOT_MESSAGES = 500


@dataclass(frozen=True)
class LookRequest:
    """一次“让模型看一眼”的请求。"""

    reason: str
    state: str
    """发起请求时的状态。"""
    requested_at: float
    message_ids: Tuple[str, ...]
    """上次查看以来新到、推动了门控的消息。"""
    trigger_message_id: Optional[str] = None
    mention_kind: Optional[str] = None
    requires_reply: bool = False
    """点名时不允许 silent。"""
    no_quote_user_ids: FrozenSet[str] = frozenset()
    """已标记机器人和防死循环冷却中的账号，不引用其消息。"""


@dataclass(frozen=True)
class LookOutcome:
    """宿主校验通过后的模型决定，只取影响状态机的两个字段。"""

    action: str
    track: str


@dataclass(frozen=True)
class _PendingQuestion:
    message_id: str
    user_id: str
    asked_at: float
    due_at: float


class SessionGate:
    """单个群会话的门控。"""

    def __init__(self, config: EngineConfig, *, rng: Optional[random.Random] = None) -> None:
        self._config = config
        self._rng = rng if rng is not None else random.Random()
        self._detector = MentionDetector(config.identity)
        self._loop_breaker = LoopBreaker(config.identity.bot_user_id, config.loop_breaker)
        self._blocked_user_ids: FrozenSet[str] = config.identity.blocked_robot_user_ids

        self._state = STATE_SCAN
        self._track_deadline = 0.0
        self._silent_looks = 0
        self._leave_looks = 0

        self._pending: List[InboundMessage] = []
        self._last_activity_at = 0.0
        self._pending_mention: Optional[Tuple[str, InboundMessage]] = None
        self._questions: Deque[_PendingQuestion] = deque()
        self._bot_message_ids: OrderedDict[str, None] = OrderedDict()
        self._in_flight: Optional[LookRequest] = None
        self._retry_request: Optional[LookRequest] = None

    @property
    def state(self) -> str:
        return self._state

    @property
    def track_deadline(self) -> float:
        return self._track_deadline

    @property
    def in_flight(self) -> Optional[LookRequest]:
        return self._in_flight

    def set_blocked_robots(self, user_ids: Iterable[str]) -> None:
        """由 is_robot 成员查询刷新已标记机器人名单。"""
        self._blocked_user_ids = frozenset(user_ids)

    def update_config(self, config: EngineConfig) -> None:
        """管理命令仅更新参数，保留当前在飞请求与消息队列。"""
        self._config = config

    def is_bot_message(self, message_id: str) -> bool:
        return message_id in self._bot_message_ids

    def observe(self, message: InboundMessage) -> None:
        """记录一条入站消息（包括 KKBK 自己发出的）。不会触发模型调用，调用方随后调用 step()。"""
        if message.user_id == self._config.identity.bot_user_id:
            self._remember_bot_message(message.message_id)
            self._loop_breaker.record(message.user_id, message.timestamp)
            self._last_activity_at = message.timestamp
            return
        if message.is_command or message.user_id in self._blocked_user_ids:
            # 命令已由入站链路处理；已标记机器人不推动门控，避免两个 bot 互相点火
            return

        mention_kind = self._detector.detect(message, self.is_bot_message)
        if message.is_notify and mention_kind is None:
            # 撤回、戳别人等系统通知只进历史，不推动门控
            return

        self._last_activity_at = message.timestamp
        if not message.is_notify:
            self._loop_breaker.record(message.user_id, message.timestamp)
            self._resolve_questions(message)

        if self._loop_breaker.in_cooldown(message.user_id, message.timestamp) and mention_kind != MENTION_AT:
            # 防死循环冷却期：对方只有直接 @ 才算点名，普通发言不推动门控
            return

        self._pending.append(message)
        if mention_kind is not None:
            self._pending_mention = (mention_kind, message)
        elif is_question(message, min_chars=self._config.gate.question_min_chars):
            due_at = message.timestamp + self._config.gate.question_wait_seconds
            self._questions.append(_PendingQuestion(message.message_id, message.user_id, message.timestamp, due_at))

    def step(self, now: float) -> Optional[LookRequest]:
        """按当前时间判断是否让模型看一眼；返回请求后会话进入在飞状态，直到 complete() 或 abort()。"""
        if self._in_flight is not None:
            return None
        if self._retry_request is not None:
            self._in_flight = self._retry_request
            self._retry_request = None
            return self._in_flight
        if self._state == STATE_TRACK and now > self._track_deadline:
            self._enter_scan()

        if self._pending_mention is not None:
            mention_kind, mention_message = self._pending_mention
            # 先按点名前的状态开请求，影子日志记录的是被点名时所处的状态
            look = self._open_look(
                REASON_MENTION,
                now,
                trigger_message_id=mention_message.message_id,
                mention_kind=mention_kind,
            )
            self._enter_track(now)
            return look

        due_question = self._pop_due_questions(now)
        if due_question is not None and self._state == STATE_SCAN:
            return self._open_look(REASON_QUESTION, now, trigger_message_id=due_question.message_id)

        if self._pending and now >= self._batch_due_at():
            if self._state == STATE_TRACK:
                return self._open_look(REASON_TRACK, now)
            if self._rng.random() < self._config.gate.explore_probability:
                return self._open_look(REASON_EXPLORE, now)
            # 静态扫描下这一批没有命中任何触发
            self._pending.clear()
        return None

    def next_wakeup(self) -> Optional[float]:
        """下一次需要调用 step() 的时间；在飞或无事可做时返回 None。"""
        if self._in_flight is not None:
            return None
        if self._retry_request is not None:
            return self._retry_request.requested_at
        candidates: List[float] = []
        if self._pending_mention is not None:
            candidates.append(self._pending_mention[1].timestamp)
        if self._questions:
            candidates.append(self._questions[0].due_at)
        if self._pending:
            candidates.append(self._batch_due_at())
        return min(candidates) if candidates else None

    def complete(self, outcome: LookOutcome, now: float) -> None:
        """模型决定已通过宿主校验并执行，按结果推进状态。"""
        look = self._in_flight
        if look is None:
            raise RuntimeError("没有在飞的查看请求")
        self._in_flight = None
        # 本次查看前已出现的问题模型都看过了，不再单独触发；只看新引擎自己的查看，不看 Maisaka 是否开口
        self._questions = deque(question for question in self._questions if question.asked_at > look.requested_at)

        if outcome.action != ACTION_SILENT:
            # KKBK 开口本身就是继续关注的新理由
            if outcome.track == TRACK_LEAVE:
                self._enter_scan()
            else:
                self._enter_track(now)
            return

        self._silent_looks += 1
        self._leave_looks = self._leave_looks + 1 if outcome.track == TRACK_LEAVE else 0
        if self._state == STATE_SCAN:
            # 静态扫描触发（问题计时、随机探索）后，模型选择留下才进入追踪
            if outcome.track == TRACK_STAY:
                self._enter_track(now)
            return
        gate_config = self._config.gate
        if self._leave_looks >= gate_config.track_leave_looks or self._silent_looks >= gate_config.track_max_silent_looks:
            self._enter_scan()

    def abort(self, *, retry: bool = True) -> None:
        """失败时保留原请求供宿主有限重试；明确耗尽重试时才丢弃，不吞掉新消息。"""
        if self._in_flight is None:
            raise RuntimeError("没有在飞的查看请求")
        self._retry_request = self._in_flight if retry else None
        self._in_flight = None

    def _open_look(
        self,
        reason: str,
        now: float,
        *,
        trigger_message_id: Optional[str] = None,
        mention_kind: Optional[str] = None,
    ) -> LookRequest:
        look = LookRequest(
            reason=reason,
            state=self._state,
            requested_at=now,
            message_ids=tuple(message.message_id for message in self._pending),
            trigger_message_id=trigger_message_id,
            mention_kind=mention_kind,
            requires_reply=reason == REASON_MENTION,
            no_quote_user_ids=self._blocked_user_ids | self._loop_breaker.cooldown_users(now),
        )
        self._pending.clear()
        self._pending_mention = None
        self._in_flight = look
        return look

    def _enter_track(self, now: float) -> None:
        self._state = STATE_TRACK
        self._track_deadline = now + self._config.gate.track_seconds
        self._silent_looks = 0
        self._leave_looks = 0

    def _enter_scan(self) -> None:
        self._state = STATE_SCAN
        self._silent_looks = 0
        self._leave_looks = 0

    def _batch_due_at(self) -> float:
        return self._last_activity_at + self._config.gate.debounce_seconds

    def _pop_due_questions(self, now: float) -> Optional[_PendingQuestion]:
        """弹出所有到期问题，返回最近的一个；追踪中到期的问题由追踪覆盖，直接丢弃。"""
        latest: Optional[_PendingQuestion] = None
        while self._questions and self._questions[0].due_at <= now:
            latest = self._questions.popleft()
        return latest

    def _resolve_questions(self, message: InboundMessage) -> None:
        """有别人发言或有人引用了这个问题，就算有人接了。"""
        if not self._questions:
            return
        self._questions = deque(
            question
            for question in self._questions
            if message.user_id == question.user_id and message.reply_to != question.message_id
        )

    def _remember_bot_message(self, message_id: str) -> None:
        self._bot_message_ids[message_id] = None
        while len(self._bot_message_ids) > MAX_REMEMBERED_BOT_MESSAGES:
            self._bot_message_ids.popitem(last=False)

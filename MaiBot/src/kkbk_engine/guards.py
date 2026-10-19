"""宿主侧护栏：限速、熔断、并发排队、防死循环。

模型只提议动作，权限和上限全部由这里的代码执行。时间均由调用方传入，便于测试。
"""

from collections import deque
from contextlib import asynccontextmanager
from typing import AsyncIterator, Deque, Dict, FrozenSet, Optional, Tuple

import asyncio

from .config import LoopBreakerConfig


class SlidingWindowLimiter:
    """按键的滑动窗口计数限速。"""

    def __init__(self, max_events: int, window_seconds: float) -> None:
        self._max_events = max_events
        self._window_seconds = window_seconds
        self._events: Dict[str, Deque[float]] = {}

    def try_acquire(self, key: str, now: float) -> bool:
        """窗口内未超限则记一次并返回 True。"""
        events = self._events.setdefault(key, deque())
        expire_before = now - self._window_seconds
        while events and events[0] <= expire_before:
            events.popleft()
        if len(events) >= self._max_events:
            return False
        events.append(now)
        return True


class CircuitBreaker:
    """连续失败熔断，冷却后自动恢复。"""

    def __init__(self, failure_threshold: int, cooldown_seconds: float) -> None:
        self._failure_threshold = failure_threshold
        self._cooldown_seconds = cooldown_seconds
        self._consecutive_failures = 0
        self._open_until = 0.0

    def allow(self, now: float) -> bool:
        return now >= self._open_until

    def record_success(self) -> None:
        self._consecutive_failures = 0

    def record_failure(self, now: float) -> None:
        self._consecutive_failures += 1
        if self._consecutive_failures >= self._failure_threshold:
            self._open_until = now + self._cooldown_seconds
            self._consecutive_failures = 0


class QueueFullError(Exception):
    """排队人数已满，应告诉对方稍后再试。"""


class QueueTimeoutError(Exception):
    """排队超时。"""


class ConcurrencyGate:
    """单种重工具的并发上限 + 有界排队 + 排队超时。"""

    def __init__(self, limit: int, max_waiting: int, wait_timeout_seconds: float) -> None:
        self._semaphore = asyncio.Semaphore(limit)
        self._max_waiting = max_waiting
        self._wait_timeout_seconds = wait_timeout_seconds
        self._waiting = 0

    @property
    def waiting(self) -> int:
        return self._waiting

    @asynccontextmanager
    async def slot(self) -> AsyncIterator[None]:
        """占用一个并发槽；队列满抛 QueueFullError，等太久抛 QueueTimeoutError。"""
        if self._semaphore.locked() and self._waiting >= self._max_waiting:
            raise QueueFullError()
        self._waiting += 1
        try:
            await asyncio.wait_for(self._semaphore.acquire(), self._wait_timeout_seconds)
        except asyncio.TimeoutError:
            raise QueueTimeoutError() from None
        finally:
            self._waiting -= 1
        try:
            yield
        finally:
            self._semaphore.release()


class LoopBreaker:
    """KKBK 与同一账号在窗口内来回超过 N 轮且无第三人插话时，对该账号进入冷却。

    不依赖 bot 识别：芙兰朵露这类未标记 bot 和活跃真人在行为上分不开，只按来回模式兜底。
    """

    def __init__(self, bot_user_id: str, config: LoopBreakerConfig) -> None:
        self._bot_user_id = bot_user_id
        self._config = config
        self._events: Deque[Tuple[float, str]] = deque()
        self._cooldown_until: Dict[str, float] = {}

    def record(self, speaker_id: str, timestamp: float) -> None:
        """记录一次发言，必要时让当前对话对象进入冷却。"""
        self._events.append((timestamp, speaker_id))
        expire_before = timestamp - self._config.window_seconds
        while self._events and self._events[0][0] < expire_before:
            self._events.popleft()
        partner, rounds = self._current_exchange()
        if partner is not None and rounds > self._config.max_rounds:
            self._cooldown_until[partner] = timestamp + self._config.cooldown_seconds

    def in_cooldown(self, user_id: str, now: float) -> bool:
        return now < self._cooldown_until.get(user_id, 0.0)

    def cooldown_users(self, now: float) -> FrozenSet[str]:
        return frozenset(user_id for user_id, until in self._cooldown_until.items() if now < until)

    def _current_exchange(self) -> Tuple[Optional[str], int]:
        """从最近一条往回数，只含 KKBK 与同一个对方的连续片段里有几轮来回。"""
        partner: Optional[str] = None
        saw_bot = False
        turns = 0
        previous: Optional[str] = None
        for _, speaker in reversed(self._events):
            if speaker == self._bot_user_id:
                saw_bot = True
            elif partner is None:
                partner = speaker
            elif speaker != partner:
                break
            if previous is not None and speaker != previous:
                turns += 1
            previous = speaker
        if partner is None or not saw_bot:
            return None, 0
        return partner, turns // 2

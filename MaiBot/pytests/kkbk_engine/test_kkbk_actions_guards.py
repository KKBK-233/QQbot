import asyncio

import pytest

from src.kkbk_engine.actions import ActionError, ValidationContext, parse_action, validate_action
from src.kkbk_engine.config import LoopBreakerConfig
from src.kkbk_engine.guards import (
    CircuitBreaker,
    ConcurrencyGate,
    LoopBreaker,
    QueueFullError,
    QueueTimeoutError,
    SlidingWindowLimiter,
)

BOT = "3976405558"


def context(requires_reply: bool = False) -> ValidationContext:
    return ValidationContext(
        requires_reply=requires_reply,
        quotable={"m1": "1001", "m2": "3889590033"},
        no_quote_user_ids=frozenset({"3889590033"}),
        max_chars=20,
    )


def error_code(raw: str, ctx: ValidationContext) -> str:
    with pytest.raises(ActionError) as info:
        validate_action(parse_action(raw), ctx)
    return info.value.code


# ---------------- 动作解析与校验 ----------------


def test_parse_plain_and_fenced() -> None:
    action = parse_action('```json\n{"action": "reply", "text": " 嗯 ", "track": "stay"}\n```')
    assert action.action == "reply" and action.text == "嗯" and action.quote is None


def test_parse_rejects_bad_input() -> None:
    for raw, code in [
        ("不是 JSON", "invalid_json"),
        ("[]", "not_object"),
        ('{"action": "reply", "track": "stay", "send_to": "x"}', "unknown_keys"),
        ('{"action": "shout", "track": "stay"}', "bad_action"),
        ('{"action": "reply", "track": "maybe"}', "bad_track"),
        ('{"action": "reply", "track": "stay", "text": 1}', "bad_type"),
    ]:
        with pytest.raises(ActionError) as info:
            parse_action(raw)
        assert info.value.code == code


def test_validate_rules() -> None:
    assert error_code('{"action": "silent", "track": "stay"}', context(requires_reply=True)) == "silent_on_mention"
    assert error_code('{"action": "silent", "track": "stay", "text": "x"}', context()) == "silent_with_payload"
    assert error_code('{"action": "reply", "track": "stay"}', context()) == "empty_reply"
    assert error_code('{"action": "escalate", "track": "stay"}', context()) == "empty_task"
    assert error_code('{"action": "reply", "track": "stay", "text": "' + "长" * 21 + '"}', context()) == "too_long"
    assert error_code('{"action": "reply", "track": "stay", "text": "@全体成员 开会"}', context()) == "at_all"
    assert error_code('{"action": "reply", "track": "stay", "text": "嗯", "quote": "m9"}', context()) == "unknown_quote"
    assert (
        error_code('{"action": "reply", "track": "stay", "text": "嗯", "quote": "m2"}', context()) == "quote_forbidden"
    )


def test_validate_accepts_good_actions() -> None:
    reply = validate_action(parse_action('{"action": "reply", "track": "stay", "text": "嗯", "quote": "m1"}'), context())
    assert reply.quote == "m1"
    escalate = validate_action(parse_action('{"action": "escalate", "track": "stay", "task": "画一只猫"}'), context())
    assert escalate.task == "画一只猫"
    silent = validate_action(parse_action('{"action": "silent", "track": "leave"}'), context())
    assert silent.track == "leave"


# ---------------- 护栏 ----------------


def test_sliding_window_limiter() -> None:
    limiter = SlidingWindowLimiter(max_events=2, window_seconds=10)
    assert limiter.try_acquire("u", 0)
    assert limiter.try_acquire("u", 1)
    assert not limiter.try_acquire("u", 2)
    assert limiter.try_acquire("other", 2)
    assert limiter.try_acquire("u", 10.5)


def test_circuit_breaker() -> None:
    breaker = CircuitBreaker(failure_threshold=3, cooldown_seconds=60)
    for _ in range(3):
        breaker.record_failure(0)
    assert not breaker.allow(30)
    assert breaker.allow(60)
    breaker.record_failure(70)
    breaker.record_success()
    breaker.record_failure(71)
    breaker.record_failure(72)
    assert breaker.allow(72)


def test_concurrency_gate_queue_full_and_timeout() -> None:
    async def scenario() -> None:
        gate = ConcurrencyGate(limit=1, max_waiting=1, wait_timeout_seconds=0.05)
        holder_entered = asyncio.Event()
        release = asyncio.Event()

        async def hold() -> None:
            async with gate.slot():
                holder_entered.set()
                await release.wait()

        async def wait_for_slot() -> None:
            async with gate.slot():
                pass

        holder = asyncio.create_task(hold())
        await holder_entered.wait()
        waiter = asyncio.create_task(wait_for_slot())
        await asyncio.sleep(0)
        assert gate.waiting == 1
        with pytest.raises(QueueFullError):
            async with gate.slot():
                pass
        with pytest.raises(QueueTimeoutError):
            await waiter
        release.set()
        await holder
        async with gate.slot():
            pass

    asyncio.run(scenario())


def alternate(breaker: LoopBreaker, partner: str, pairs: int, start: float = 0.0) -> float:
    t = start
    for _ in range(pairs):
        breaker.record(partner, t)
        breaker.record(BOT, t + 1)
        t += 2
    return t


def test_loop_breaker_cools_down_back_and_forth() -> None:
    breaker = LoopBreaker(BOT, LoopBreakerConfig())
    t = alternate(breaker, "9999", 12)
    assert breaker.in_cooldown("9999", t)
    assert "9999" in breaker.cooldown_users(t)
    assert not breaker.in_cooldown("9999", t + 700)


def test_loop_breaker_reset_by_third_person() -> None:
    breaker = LoopBreaker(BOT, LoopBreakerConfig())
    t = alternate(breaker, "9999", 6)
    breaker.record("1001", t)
    t = alternate(breaker, "9999", 6, start=t + 1)
    assert not breaker.in_cooldown("9999", t)


def test_loop_breaker_ignores_slow_exchange() -> None:
    breaker = LoopBreaker(BOT, LoopBreakerConfig())
    t = 0.0
    for _ in range(12):
        breaker.record("9999", t)
        breaker.record(BOT, t + 1)
        t += 100
    assert not breaker.in_cooldown("9999", t)

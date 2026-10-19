from typing import Optional, Tuple

from src.kkbk_engine.config import EngineConfig
from src.kkbk_engine.gate import (
    REASON_EXPLORE,
    REASON_MENTION,
    REASON_QUESTION,
    REASON_TRACK,
    STATE_SCAN,
    STATE_TRACK,
    LookOutcome,
    SessionGate,
)
from src.kkbk_engine.inbound import InboundMessage
from src.kkbk_engine.triggers import (
    MENTION_AT,
    MENTION_NAME,
    MENTION_POKE,
    MENTION_QUOTE_BOT,
    POKE_SUFFIX,
    MentionDetector,
    is_question,
)

BOT = "3976405558"
ROBOT = "3889590033"


class FixedRandom:
    """random() 恒返回给定值，用于控制随机探索。"""

    def __init__(self, value: float) -> None:
        self.value = value

    def random(self) -> float:
        return self.value


_counter = 0


def msg(
    t: float,
    text: str = "随便聊聊",
    *,
    user: str = "1001",
    nickname: str = "群友",
    mentions: Tuple[str, ...] = (),
    reply_to: Optional[str] = None,
    notify: bool = False,
    message_id: Optional[str] = None,
) -> InboundMessage:
    global _counter
    _counter += 1
    return InboundMessage(
        message_id=message_id or f"m{_counter}",
        timestamp=t,
        user_id=user,
        nickname=nickname,
        text=text,
        visible_text=text,
        mentions=mentions,
        reply_to=reply_to,
        is_notify=notify,
    )


def make_gate(explore: float = 1.0) -> SessionGate:
    """explore 是 rng 返回值：1.0 表示永不探索，0.0 表示每批都探索。"""
    return SessionGate(EngineConfig(), rng=FixedRandom(explore))  # type: ignore[arg-type]


SILENT_STAY = LookOutcome(action="silent", track="stay")
SILENT_LEAVE = LookOutcome(action="silent", track="leave")
REPLY_STAY = LookOutcome(action="reply", track="stay")


# ---------------- 点名判断 ----------------


def test_mention_kinds() -> None:
    detector = MentionDetector(EngineConfig().identity)
    never = lambda _: False  # noqa: E731
    assert detector.detect(msg(0, "看这个", mentions=(BOT,)), never) == MENTION_AT
    assert detector.detect(msg(0, "嗯", reply_to="b1"), lambda mid: mid == "b1") == MENTION_QUOTE_BOT
    assert detector.detect(msg(0, "娜娜在吗"), never) == MENTION_NAME
    assert detector.detect(msg(0, "KKBK 帮我画"), never) == MENTION_NAME
    assert detector.detect(msg(0, "蕾娜是谁"), never) is None
    assert detector.detect(msg(0, "嗯", reply_to="other"), never) is None


def test_poke_only_counts_when_target_is_bot() -> None:
    detector = MentionDetector(EngineConfig().identity)
    never = lambda _: False  # noqa: E731
    poke_bot = msg(0, f"七夜酱戳了戳KKBK{POKE_SUFFIX}", nickname="七夜酱", notify=True)
    poke_owner = msg(0, f"七夜酱戳了戳吉祥物{POKE_SUFFIX}", nickname="七夜酱", notify=True)
    # 发起人名字里含唤醒名，戳的却是别人
    poke_by_fan = msg(0, f"伊蕾娜粉丝戳了戳qwert{POKE_SUFFIX}", nickname="伊蕾娜粉丝", notify=True)
    recall = msg(0, "[事件-群消息撤回] KKBK 撤回了一条消息", notify=True)
    assert detector.detect(poke_bot, never) == MENTION_POKE
    assert detector.detect(poke_owner, never) is None
    assert detector.detect(poke_by_fan, never) is None
    assert detector.detect(recall, never) is None


def test_is_question() -> None:
    assert is_question(msg(0, "这个怎么弄啊"), min_chars=4)
    assert not is_question(msg(0, "咋了"), min_chars=4)
    assert not is_question(msg(0, "这个怎么弄啊", mentions=("1002",)), min_chars=4)
    assert not is_question(msg(0, "今天天气不错"), min_chars=4)


# ---------------- 状态机 ----------------


def test_mention_looks_immediately_and_records_previous_state() -> None:
    gate = make_gate()
    gate.observe(msg(0, "伊蕾娜", message_id="x1"))
    look = gate.step(0)
    assert look is not None
    assert look.reason == REASON_MENTION
    assert look.state == STATE_SCAN
    assert look.requires_reply
    assert look.trigger_message_id == "x1"
    assert gate.state == STATE_TRACK


def test_tracking_looks_after_debounce_and_leaves_after_two_leaves() -> None:
    gate = make_gate()
    gate.observe(msg(0, "娜娜"))
    gate.step(0)
    gate.complete(REPLY_STAY, 1)

    gate.observe(msg(10))
    assert gate.step(10) is None
    assert gate.next_wakeup() == 13
    look = gate.step(15)
    assert look is not None and look.reason == REASON_TRACK and not look.requires_reply
    gate.complete(SILENT_LEAVE, 15)
    assert gate.state == STATE_TRACK

    gate.observe(msg(20))
    gate.step(25)
    gate.complete(SILENT_LEAVE, 25)
    assert gate.state == STATE_SCAN


def test_tracking_times_out() -> None:
    gate = make_gate()
    gate.observe(msg(0, "娜娜"))
    gate.step(0)
    gate.complete(REPLY_STAY, 0)
    gate.observe(msg(200))
    assert gate.step(205) is None
    assert gate.state == STATE_SCAN


def test_tracking_exits_after_repeated_silence() -> None:
    gate = make_gate()
    gate.observe(msg(0, "娜娜"))
    gate.step(0)
    gate.complete(REPLY_STAY, 0)
    for i in range(4):
        gate.observe(msg(10 + i * 10))
        assert gate.step(15 + i * 10) is not None
        gate.complete(SILENT_STAY, 15 + i * 10)
    assert gate.state == STATE_SCAN


def test_messages_during_flight_merge_into_next_batch() -> None:
    gate = make_gate()
    gate.observe(msg(0, "娜娜"))
    gate.step(0)
    gate.observe(msg(1, "接着说", message_id="late"))
    assert gate.step(6) is None
    assert gate.next_wakeup() is None
    gate.complete(REPLY_STAY, 7)
    look = gate.step(7)
    assert look is not None and look.reason == REASON_TRACK
    assert "late" in look.message_ids


def test_question_timer_fires_when_unanswered() -> None:
    gate = make_gate()
    gate.observe(msg(0, "这个怎么弄啊", user="1001", message_id="q1"))
    assert gate.step(5) is None
    assert gate.next_wakeup() == 60
    look = gate.step(60)
    assert look is not None
    assert look.reason == REASON_QUESTION and look.trigger_message_id == "q1"
    gate.complete(SILENT_STAY, 60)
    assert gate.state == STATE_TRACK


def test_question_timer_cancelled_when_someone_answers() -> None:
    gate = make_gate()
    gate.observe(msg(0, "这个怎么弄啊", user="1001"))
    gate.step(5)
    gate.observe(msg(30, "我也不知道", user="1002"))
    gate.step(35)
    assert gate.step(60) is None


def test_question_timer_cancelled_after_engine_looked() -> None:
    gate = make_gate()
    gate.observe(msg(0, "这个怎么弄啊", user="1001"))
    gate.observe(msg(10, "娜娜", user="1002"))
    assert gate.step(10) is not None
    gate.complete(LookOutcome(action="reply", track="leave"), 11)
    assert gate.state == STATE_SCAN
    # 问题已在点名那次查看里被模型看过，回到静态扫描后 60 秒到期也不再单独触发
    assert gate.step(60) is None


def test_question_timer_not_cancelled_by_other_bot_speech() -> None:
    """影子期 Maisaka 也会以 KKBK 身份发言，不能据此取消新引擎的问题计时。"""
    gate = make_gate()
    gate.observe(msg(0, "这个怎么弄啊", user="1001"))
    gate.step(5)
    gate.observe(msg(30, "这样弄", user=BOT))
    gate.step(35)
    look = gate.step(60)
    assert look is not None and look.reason == REASON_QUESTION


def test_same_user_follow_up_does_not_answer_question() -> None:
    gate = make_gate()
    gate.observe(msg(0, "这个怎么弄啊", user="1001"))
    gate.step(5)
    gate.observe(msg(30, "有人吗", user="1001"))
    gate.step(35)
    look = gate.step(60)
    assert look is not None and look.reason == REASON_QUESTION


def test_explore_triggers_with_probability() -> None:
    gate = make_gate(explore=0.0)
    gate.observe(msg(0))
    look = gate.step(5)
    assert look is not None and look.reason == REASON_EXPLORE

    quiet = make_gate(explore=1.0)
    quiet.observe(msg(0))
    assert quiet.step(5) is None
    assert quiet.next_wakeup() is None


def test_blocked_robot_does_not_drive_gate() -> None:
    gate = make_gate(explore=0.0)
    gate.observe(msg(0, "伊蕾娜", user=ROBOT, mentions=(BOT,)))
    assert gate.step(0) is None
    assert gate.step(10) is None


def test_blocked_robot_cannot_be_quoted() -> None:
    gate = make_gate()
    gate.observe(msg(0, "娜娜"))
    look = gate.step(0)
    assert look is not None and ROBOT in look.no_quote_user_ids


def test_quote_of_own_message_is_mention() -> None:
    gate = make_gate()
    gate.observe(msg(0, "我说的", user=BOT, message_id="b1"))
    gate.observe(msg(1, "真的吗", reply_to="b1"))
    look = gate.step(1)
    assert look is not None and look.mention_kind == MENTION_QUOTE_BOT


def test_complete_without_flight_raises() -> None:
    gate = make_gate()
    try:
        gate.complete(SILENT_STAY, 0)
    except RuntimeError:
        return
    raise AssertionError("应抛出 RuntimeError")


def test_abort_keeps_mention_and_new_messages_for_retry() -> None:
    """失败重试仍处理原点名，期间新来的另一条点名不能被清掉。"""
    gate = make_gate()
    gate.observe(msg(0, "娜娜", message_id="first"))
    original = gate.step(0)
    gate.observe(msg(1, "娜娜再看一下", message_id="next"))
    gate.abort()
    assert gate.next_wakeup() is not None
    assert gate.step(2) == original
    gate.complete(REPLY_STAY, 3)
    assert gate.step(3).trigger_message_id == "next"


def test_abort_can_explicitly_exhaust_retry() -> None:
    """有限重试耗尽后结束原请求，不能在后台无限重放。"""
    gate = make_gate()
    gate.observe(msg(0, "娜娜"))
    gate.step(0)
    gate.abort(retry=False)
    assert gate.step(1) is None
    assert gate.next_wakeup() is None

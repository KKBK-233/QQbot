"""离线回放：用新引擎的门控状态机重放导出的群聊记录，统计调用次数和覆盖率。

不调用模型：点名时模拟 reply/stay，其余模拟 silent/stay，状态只由宿主规则推进。
标签沿用 gate-replay：点名、MaiBot 实际的自发插话（冷场/热聊）、120 秒没人接的问题。
覆盖：标签消息出现后 90 秒内有一次查看，且该消息在最近 30 条上下文里。

    cd MaiBot
    python -X utf8 -m src.kkbk_engine.replay_eval path/to/messages.jsonl

每次改状态机参数都要跑一遍（pytests/kkbk_engine/test_kkbk_replay_regression.py）。
"""

from collections import Counter
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Dict, List, Optional, Sequence, Tuple

import bisect
import json
import random
import sys

from .config import EngineConfig
from .gate import REASON_MENTION, LookOutcome, LookRequest, SessionGate
from .inbound import InboundMessage, from_export_row
from .triggers import MentionDetector, is_question

CN_TZ = timezone(timedelta(hours=8))
DAY_CUT_HOUR = 4
"""夜间复盘时刻：北京时间 04:00 切日。"""
COVER_SECONDS = 90.0
COVER_CONTEXT_MESSAGES = 30
UNANSWERED_WAIT_SECONDS = 120.0
DEFAULT_SEED = 7


@dataclass(frozen=True)
class ReplayLook:
    timestamp: float
    last_index: int
    reason: str


@dataclass(frozen=True)
class Label:
    index: int
    timestamp: float
    kind: str


def load_export(path: Path) -> List[InboundMessage]:
    with path.open(encoding="utf-8") as file:
        rows = [json.loads(line) for line in file]
    rows.sort(key=lambda row: (row["epoch_seconds"], row["row_id"]))
    return [from_export_row(row) for row in rows]


def day_of(timestamp: float) -> str:
    return (datetime.fromtimestamp(timestamp, CN_TZ) - timedelta(hours=DAY_CUT_HOUR)).date().isoformat()


def _simulated_outcome(look: LookRequest) -> LookOutcome:
    if look.reason == REASON_MENTION:
        return LookOutcome(action="reply", track="stay")
    return LookOutcome(action="silent", track="stay")


def simulate(messages: Sequence[InboundMessage], config: EngineConfig, *, seed: int) -> List[ReplayLook]:
    """按消息时间驱动门控，消息之间的计时器（防抖、问题计时）按 next_wakeup 补触发。"""
    gate = SessionGate(config, rng=random.Random(seed))
    looks: List[ReplayLook] = []
    last_index = -1

    def drain(now: float) -> None:
        while (look := gate.step(now)) is not None:
            looks.append(ReplayLook(now, last_index, look.reason))
            gate.complete(_simulated_outcome(look), now)

    for index, message in enumerate(messages):
        while (wakeup := gate.next_wakeup()) is not None and wakeup <= message.timestamp:
            drain(wakeup)
        gate.observe(message)
        last_index = index
        drain(message.timestamp)
    while (wakeup := gate.next_wakeup()) is not None:
        drain(wakeup)
    return looks


def mention_kinds(messages: Sequence[InboundMessage], config: EngineConfig) -> List[Optional[str]]:
    """逐条计算点名类型；引用 bot 依赖此前见过的 bot 消息编号。"""
    detector = MentionDetector(config.identity)
    bot_user_id = config.identity.bot_user_id
    bot_message_ids: set[str] = set()
    kinds: List[Optional[str]] = []
    for message in messages:
        if message.user_id == bot_user_id:
            bot_message_ids.add(message.message_id)
            kinds.append(None)
        else:
            kinds.append(detector.detect(message, bot_message_ids.__contains__))
    return kinds


def _bot_target(messages: Sequence[InboundMessage], index_by_id: Dict[str, int], index: int, bot: str) -> Optional[int]:
    """机器人这条发言在回应谁：引用的人类消息，否则前一条人类消息。"""
    message = messages[index]
    if message.reply_to and (target := index_by_id.get(message.reply_to)) is not None:
        if messages[target].user_id != bot:
            return target
    cursor = index - 1
    while cursor >= 0 and messages[cursor].user_id == bot:
        cursor -= 1
    return cursor if cursor >= 0 else None


def label_spontaneous(
    messages: Sequence[InboundMessage], kinds: Sequence[Optional[str]], bot: str
) -> List[Label]:
    """MaiBot 实际的自发插话：回应的目标不是对它的点名；60 秒内连续的机器人发言合并成一次。"""
    index_by_id = {message.message_id: index for index, message in enumerate(messages)}
    labels: List[Label] = []
    last_bot_at = -1e18
    turn_open: Optional[str] = None
    for index, message in enumerate(messages):
        if message.user_id != bot:
            continue
        target = _bot_target(messages, index_by_id, index, bot)
        new_turn = message.timestamp - last_bot_at > 60
        last_bot_at = message.timestamp
        if target is None:
            continue
        if new_turn:
            turn_open = None
        if kinds[target]:
            turn_open = "hard"
            continue
        if turn_open is not None:
            continue
        turn_open = "spont"
        # 冷启动：此前 10 分钟机器人没说过话、也没人点名
        target_at = messages[target].timestamp
        warm = any(
            (messages[k].user_id == bot or kinds[k]) and target_at - messages[k].timestamp < 600
            for k in range(max(0, target - 80), target)
        )
        labels.append(Label(target, target_at, "spont_warm" if warm else "spont_cold"))
    return labels


def label_unanswered_questions(
    messages: Sequence[InboundMessage], kinds: Sequence[Optional[str]], config: EngineConfig
) -> List[Label]:
    bot = config.identity.bot_user_id
    labels: List[Label] = []
    for index, message in enumerate(messages):
        if message.user_id == bot or kinds[index]:
            continue
        if not is_question(message, min_chars=config.gate.question_min_chars):
            continue
        answered = False
        for later in messages[index + 1 :]:
            if later.timestamp - message.timestamp > UNANSWERED_WAIT_SECONDS:
                break
            if later.user_id != bot and (later.user_id != message.user_id or later.reply_to == message.message_id):
                answered = True
                break
        if not answered:
            labels.append(Label(index, message.timestamp, "unanswered_q"))
    return labels


def coverage(looks: Sequence[ReplayLook], labels: Sequence[Label]) -> Tuple[int, int]:
    times = [look.timestamp for look in looks]
    covered = 0
    for label in labels:
        cursor = bisect.bisect_left(times, label.timestamp - 1e-6)
        while cursor < len(looks) and looks[cursor].timestamp <= label.timestamp + COVER_SECONDS:
            if looks[cursor].last_index - label.index <= COVER_CONTEXT_MESSAGES:
                covered += 1
                break
            cursor += 1
    return covered, len(labels)


def count_every_batch_calls(
    messages: Sequence[InboundMessage], kinds: Sequence[Optional[str]], config: EngineConfig
) -> int:
    """“每批都看”的调用数：人类消息后间隔超过防抖，或是点名。"""
    bot = config.identity.bot_user_id
    calls = 0
    for index, message in enumerate(messages):
        if message.user_id == bot:
            continue
        next_at = messages[index + 1].timestamp if index + 1 < len(messages) else float("inf")
        if next_at - message.timestamp >= config.gate.debounce_seconds or kinds[index]:
            calls += 1
    return calls


def evaluate(path: Path, config: Optional[EngineConfig] = None, *, seed: int = DEFAULT_SEED) -> Dict[str, object]:
    """第一天不完整、前两天作冷启动，评估从第 3 个自然日开始（与 gate-replay 一致）。"""
    config = config or EngineConfig()
    all_messages = load_export(path)
    days = sorted({day_of(message.timestamp) for message in all_messages})
    test_days = set(days[2:])
    messages = [message for message in all_messages if day_of(message.timestamp) in test_days]

    bot = config.identity.bot_user_id
    kinds = mention_kinds(messages, config)
    looks = simulate(messages, config, seed=seed)
    spontaneous = label_spontaneous(messages, kinds, bot)
    labels = {
        "hard": [Label(i, m.timestamp, "hard") for i, m in enumerate(messages) if kinds[i]],
        "spont_cold": [label for label in spontaneous if label.kind == "spont_cold"],
        "spont_warm": [label for label in spontaneous if label.kind == "spont_warm"],
        "spont": spontaneous,
        "unanswered_q": label_unanswered_questions(messages, kinds, config),
    }
    every_batch = count_every_batch_calls(messages, kinds, config)
    return {
        "test_days": sorted(test_days),
        "messages": len(messages),
        "calls": len(looks),
        "every_batch_calls": every_batch,
        "call_ratio": len(looks) / max(every_batch, 1),
        "reasons": dict(Counter(look.reason for look in looks)),
        "coverage": {name: coverage(looks, group) for name, group in labels.items()},
    }


def main(argv: Sequence[str]) -> None:
    result = evaluate(Path(argv[1]))
    print(f"测试日 {result['test_days'][0]} → {result['test_days'][-1]}，消息 {result['messages']} 条")
    print(f"调用 {result['calls']} 次 / 每批都看 {result['every_batch_calls']} 次 = {result['call_ratio']:.0%}")
    print(f"触发原因 {result['reasons']}")
    for name, (covered, total) in result["coverage"].items():
        print(f"  {name:13s} {covered}/{total} = {covered / max(total, 1):.0%}")


if __name__ == "__main__":
    main(sys.argv)

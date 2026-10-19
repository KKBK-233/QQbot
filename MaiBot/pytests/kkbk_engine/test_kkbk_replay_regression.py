"""门控回放回归：每次改状态机参数都要跑。

默认读取主群 14 天导出；可用环境变量 KKBK_REPLAY_EXPORT 指定其他 messages.jsonl，文件不存在时跳过。
"""

from pathlib import Path

import os

import pytest

from src.kkbk_engine.replay_eval import evaluate

DEFAULT_EXPORT = (
    Path(__file__).resolve().parents[3].parent
    / "QQbot/data/chat-exports/group_1022382380_14d_20261018_052138/messages.jsonl"
)
EXPORT = Path(os.environ.get("KKBK_REPLAY_EXPORT", str(DEFAULT_EXPORT)))


@pytest.mark.skipif(not EXPORT.exists(), reason=f"回放数据不存在: {EXPORT}")
def test_gate_replay_meets_v1_targets() -> None:
    result = evaluate(EXPORT)
    coverage = result["coverage"]

    def rate(name: str) -> float:
        covered, total = coverage[name]
        return covered / max(total, 1)

    # 点名必须每次都看到
    assert rate("hard") == 1.0
    # 设计目标：自发插话时刻覆盖不低于 80%
    assert rate("spont") >= 0.80
    assert rate("unanswered_q") >= 0.95
    # 调用量约为“每批都看”的一半，留一点余量
    assert result["call_ratio"] <= 0.60

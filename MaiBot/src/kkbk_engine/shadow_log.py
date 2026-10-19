"""影子日志：记录新引擎“会怎么做”，供与 Maisaka 实际行为并排对比。

每天一个 JSONL 文件（北京时间）。写文件是同步 IO，事件循环里请经 asyncio.to_thread 调用。
"""

from dataclasses import asdict, dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, Optional

import json
import threading

CN_TZ = timezone(timedelta(hours=8))


@dataclass(frozen=True)
class ShadowRecord:
    timestamp: float
    session_id: str
    group_id: str
    state: str
    reason: str
    mention_kind: Optional[str]
    trigger_message_id: Optional[str]
    action: str
    """silent / reply / escalate；调用失败或动作被拒绝时为 error。"""
    track: str
    text: str
    quote: Optional[str]
    task: str
    model_reason: str
    validation_error: Optional[str]
    input_tokens: int
    output_tokens: int
    cache_hit_tokens: int
    cache_miss_tokens: int
    latency_ms: int
    maisaka_actual: Optional[Dict[str, Any]] = None
    """同一窗口 Maisaka 的实际行为，由对比导出脚本回填。"""


class ShadowLogger:
    def __init__(self, directory: Path) -> None:
        self._directory = directory
        self._lock = threading.Lock()

    def path_for(self, timestamp: float) -> Path:
        day = datetime.fromtimestamp(timestamp, CN_TZ).strftime("%Y-%m-%d")
        return self._directory / f"shadow-{day}.jsonl"

    def write(self, record: ShadowRecord) -> Path:
        path = self.path_for(record.timestamp)
        line = json.dumps(asdict(record), ensure_ascii=False)
        with self._lock:
            self._directory.mkdir(parents=True, exist_ok=True)
            with path.open("a", encoding="utf-8", newline="\n") as file:
                file.write(line + "\n")
        return path

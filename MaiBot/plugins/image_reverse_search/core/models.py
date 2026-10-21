"""图片反查插件的与 SDK 无关的数据模型。"""

from dataclasses import dataclass, field
from typing import Any, Dict, List, Mapping, Optional, Tuple

ENGINE_SAUCENAO = "saucenao"
ENGINE_ASCII2D = "ascii2d"
ENGINE_IQDB = "iqdb"
ENGINE_TRACEMOE = "tracemoe"

ALLOWED_ENGINES: Tuple[str, ...] = (
    ENGINE_SAUCENAO,
    ENGINE_ASCII2D,
    ENGINE_IQDB,
    ENGINE_TRACEMOE,
)

DEFAULT_ILLUSTRATION_ENGINES: Tuple[str, ...] = (
    ENGINE_SAUCENAO,
    ENGINE_ASCII2D,
    ENGINE_IQDB,
)

DEFAULT_ANIME_ENGINES: Tuple[str, ...] = (
    ENGINE_TRACEMOE,
    ENGINE_SAUCENAO,
    ENGINE_ASCII2D,
    ENGINE_IQDB,
)

ENGINE_LABELS: Dict[str, str] = {
    ENGINE_SAUCENAO: "SauceNAO",
    ENGINE_ASCII2D: "ascii2d",
    ENGINE_IQDB: "IQDB",
    ENGINE_TRACEMOE: "trace.moe",
}

MODE_ILLUSTRATION = "illustration"
MODE_ANIME = "anime"

MODE_LABELS: Dict[str, str] = {
    MODE_ILLUSTRATION: "二次元插画",
    MODE_ANIME: "番剧截图",
}


class EngineStatus:
    """引擎单次尝试的状态常量。"""

    HIT = "hit"
    LOW_SCORE = "low_score"
    NO_RESULT = "no_result"
    TIMEOUT = "timeout"
    ERROR = "error"
    RATE_LIMITED = "rate_limited"
    SKIPPED = "skipped"


@dataclass(frozen=True)
class SearchSettings:
    """一次图片反查使用的可配置参数。"""

    illustration_engines: Tuple[str, ...] = DEFAULT_ILLUSTRATION_ENGINES
    anime_engines: Tuple[str, ...] = DEFAULT_ANIME_ENGINES
    hit_threshold: float = 80.0
    engine_timeout: float = 15.0
    engine_timeouts: Mapping[str, float] = field(default_factory=dict)
    max_low_results: int = 5
    ascii2d_implicit_score: float = 85.0
    max_image_bytes: int = 10 * 1024 * 1024

    def __post_init__(self) -> None:
        """校验配置，避免错误配置被静默接受。"""

        self._validate_order("illustration_engines", self.illustration_engines)
        self._validate_order("anime_engines", self.anime_engines)
        if not 0 < self.hit_threshold <= 100:
            raise ValueError("hit_threshold 必须位于 (0, 100] 区间")
        if self.engine_timeout <= 0:
            raise ValueError("engine_timeout 必须大于 0")
        if self.max_low_results < 1:
            raise ValueError("max_low_results 必须大于等于 1")
        if not 0 <= self.ascii2d_implicit_score <= 100:
            raise ValueError("ascii2d_implicit_score 必须位于 [0, 100] 区间")
        if self.max_image_bytes < 1:
            raise ValueError("max_image_bytes 必须大于 0")
        for engine, timeout in self.engine_timeouts.items():
            if engine not in ALLOWED_ENGINES:
                raise ValueError(f"engine_timeouts 包含未知引擎: {engine}")
            if timeout <= 0:
                raise ValueError(f"engine_timeouts.{engine} 必须大于 0")

    @staticmethod
    def _validate_order(field_name: str, order: Tuple[str, ...]) -> None:
        """校验引擎顺序非空、无重复且均为受支持引擎。"""

        if not order:
            raise ValueError(f"{field_name} 不能为空")
        if len(set(order)) != len(order):
            raise ValueError(f"{field_name} 存在重复引擎")
        unknown = [engine for engine in order if engine not in ALLOWED_ENGINES]
        if unknown:
            raise ValueError(f"{field_name} 包含未知引擎: {', '.join(unknown)}")

    def engines_for_mode(self, mode: str) -> Tuple[str, ...]:
        """按查询模式返回引擎顺序。"""

        if mode == MODE_ANIME:
            return self.anime_engines
        if mode == MODE_ILLUSTRATION:
            return self.illustration_engines
        raise ValueError(f"未知查询模式: {mode}")

    def timeout_for(self, engine: str) -> float:
        """返回指定引擎的独立超时时间。"""

        return float(self.engine_timeouts.get(engine, self.engine_timeout))


@dataclass(frozen=True)
class SearchResult:
    """统一格式的单条图片反查结果。"""

    engine: str
    title: str
    author: str
    source_url: str
    similarity: Optional[float]
    score: float
    detail: str = ""
    thumbnail: str = ""

    @property
    def engine_label(self) -> str:
        """返回展示用引擎名称。"""

        return ENGINE_LABELS.get(self.engine, self.engine)

    @property
    def similarity_label(self) -> str:
        """返回展示用相似度：引擎未提供评分时明确说明。"""

        if self.similarity is None:
            return "未知（该引擎未提供评分）"
        return f"{self.similarity:.1f}%"


@dataclass
class EngineAttempt:
    """单个引擎的尝试记录。"""

    engine: str
    status: str
    elapsed_ms: int
    result_count: int = 0
    best_score: Optional[float] = None
    reason: str = ""

    @property
    def engine_label(self) -> str:
        """返回展示用引擎名称。"""

        return ENGINE_LABELS.get(self.engine, self.engine)

    def to_dict(self) -> Dict[str, Any]:
        """转换为可序列化字典。"""

        return {
            "engine": self.engine,
            "label": self.engine_label,
            "status": self.status,
            "elapsed_ms": self.elapsed_ms,
            "result_count": self.result_count,
            "best_score": self.best_score,
            "reason": self.reason,
        }


@dataclass(frozen=True)
class EngineSearchOutcome:
    """引擎返回的结构化结果；用于缺 key 等“跳过但不抛异常”的场景。"""

    results: List[SearchResult] = field(default_factory=list)
    skipped: bool = False
    reason: str = ""


@dataclass
class SearchReport:
    """一次图片反查的完整报告。"""

    mode: str
    threshold: float
    attempts: List[EngineAttempt]
    hit: Optional[SearchResult] = None
    low_results: List[SearchResult] = field(default_factory=list)

    @property
    def mode_label(self) -> str:
        """返回展示用模式名称。"""

        return MODE_LABELS.get(self.mode, self.mode)

    @property
    def hit_engine(self) -> Optional[str]:
        """返回命中的引擎名称。"""

        return self.hit.engine if self.hit is not None else None

    def to_dict(self) -> Dict[str, Any]:
        """转换为可序列化字典，便于工具结果或测试断言。"""

        return {
            "mode": self.mode,
            "mode_label": self.mode_label,
            "threshold": self.threshold,
            "hit": self._result_to_dict(self.hit) if self.hit is not None else None,
            "low_results": [self._result_to_dict(result) for result in self.low_results],
            "attempts": [attempt.to_dict() for attempt in self.attempts],
        }

    @staticmethod
    def _result_to_dict(result: SearchResult) -> Dict[str, Any]:
        """转换单条结果。"""

        return {
            "engine": result.engine,
            "label": result.engine_label,
            "title": result.title,
            "author": result.author,
            "source_url": result.source_url,
            "similarity": result.similarity,
            "score": result.score,
            "detail": result.detail,
        }

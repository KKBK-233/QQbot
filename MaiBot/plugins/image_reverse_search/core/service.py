"""多引擎按序回退调度。"""

from dataclasses import replace
from typing import Iterable, List, Optional, Protocol, Sequence, Tuple

import asyncio
import logging
import time

from .errors import EngineRateLimited, EngineRequestError, EngineTimeout, EngineUnavailable
from .image import ImageInput
from .models import (
    ALLOWED_ENGINES,
    MODE_ANIME,
    MODE_ILLUSTRATION,
    EngineAttempt,
    EngineSearchOutcome,
    EngineStatus,
    SearchReport,
    SearchResult,
    SearchSettings,
)
from .security import redact_text, safe_reason


class SearchBackend(Protocol):
    """图片反查后端协议，便于测试时注入假引擎。"""

    async def search(
        self,
        engine: str,
        image: ImageInput,
        timeout: float,
    ) -> List[SearchResult] | EngineSearchOutcome:
        """调用单个引擎并返回统一结果或结构化跳过状态。"""


class ImageSearchService:
    """按配置顺序逐个调用引擎，命中即停，失败自动降级。"""

    def __init__(
        self,
        backend: SearchBackend,
        settings: Optional[SearchSettings] = None,
        logger: Optional[logging.Logger] = None,
        secrets: Iterable[str] = (),
    ) -> None:
        """保存后端、配置与脱敏用密钥列表。"""

        self._backend = backend
        self._settings = settings or SearchSettings()
        self._logger = logger or logging.getLogger(__name__)
        self._secrets: Tuple[str, ...] = tuple(str(secret) for secret in secrets if str(secret))

    @property
    def settings(self) -> SearchSettings:
        """返回当前搜索配置。"""

        return self._settings

    async def search(
        self,
        image: ImageInput,
        mode: str = MODE_ILLUSTRATION,
        engines: Optional[Sequence[str]] = None,
        threshold: Optional[float] = None,
    ) -> SearchReport:
        """执行一次图片反查。

        Args:
            image: 待反查图片。
            mode: ``illustration`` 或 ``anime``。
            engines: 可选的本次引擎顺序覆盖；留空使用配置。
            threshold: 可选的本次命中阈值覆盖。

        Returns:
            SearchReport: 包含命中结果或低分汇总，以及全部引擎尝试记录。
        """

        normalized_mode = str(mode or MODE_ILLUSTRATION).strip().lower()
        if normalized_mode not in {MODE_ILLUSTRATION, MODE_ANIME}:
            raise ValueError(f"未知查询模式: {mode}")
        order = self._resolve_order(normalized_mode, engines)
        active_threshold = self._settings.hit_threshold if threshold is None else float(threshold)
        if not 0 < active_threshold <= 100:
            raise ValueError("命中阈值必须位于 (0, 100] 区间")

        image_ref = image.fingerprint()
        self._logger.debug(
            "图片反查开始: image=%s mode=%s engines=%s threshold=%.1f",
            image_ref,
            normalized_mode,
            ",".join(order),
            active_threshold,
        )

        attempts: List[EngineAttempt] = []
        low_pool: List[SearchResult] = []

        for engine in order:
            timeout = self._settings.timeout_for(engine)
            started = time.monotonic()
            try:
                raw_results = await asyncio.wait_for(self._backend.search(engine, image, timeout), timeout=timeout)
            except TimeoutError:
                attempts.append(self._attempt(engine, EngineStatus.TIMEOUT, started, reason=f"超过 {timeout:g}s"))
                self._log_attempt(image_ref, attempts[-1])
                continue
            except EngineUnavailable as exc:
                attempts.append(self._attempt(engine, EngineStatus.SKIPPED, started, reason=self._safe(str(exc))))
                self._log_attempt(image_ref, attempts[-1])
                continue
            except EngineTimeout as exc:
                attempts.append(self._attempt(engine, EngineStatus.TIMEOUT, started, reason=self._safe(str(exc))))
                self._log_attempt(image_ref, attempts[-1])
                continue
            except EngineRateLimited as exc:
                attempts.append(self._attempt(engine, EngineStatus.RATE_LIMITED, started, reason=self._safe(str(exc))))
                self._log_attempt(image_ref, attempts[-1])
                continue
            except EngineRequestError as exc:
                attempts.append(self._attempt(engine, EngineStatus.ERROR, started, reason=self._safe(str(exc))))
                self._log_attempt(image_ref, attempts[-1])
                continue
            except Exception as exc:
                # 第三方异常正文可能包含响应内容，只记录异常类型，避免泄露。
                attempts.append(
                    self._attempt(
                        engine,
                        EngineStatus.ERROR,
                        started,
                        reason=f"请求失败（{type(exc).__name__}）",
                    )
                )
                self._log_attempt(image_ref, attempts[-1])
                continue

            outcome_reason = ""
            if isinstance(raw_results, EngineSearchOutcome):
                if raw_results.skipped:
                    attempts.append(
                        self._attempt(
                            engine,
                            EngineStatus.SKIPPED,
                            started,
                            reason=self._safe(raw_results.reason),
                        )
                    )
                    self._log_attempt(image_ref, attempts[-1])
                    continue
                outcome_reason = self._safe(raw_results.reason)
                raw_results = raw_results.results

            results = self._sanitize_results(raw_results)
            results.sort(key=lambda result: result.score, reverse=True)
            best = results[0] if results else None
            if best is not None and best.score >= active_threshold:
                attempts.append(
                    self._attempt(
                        engine,
                        EngineStatus.HIT,
                        started,
                        result_count=len(results),
                        best_score=best.score,
                        reason=outcome_reason,
                    )
                )
                self._log_attempt(image_ref, attempts[-1])
                return SearchReport(
                    mode=normalized_mode,
                    threshold=active_threshold,
                    attempts=attempts,
                    hit=best,
                    low_results=[],
                )

            if results:
                attempts.append(
                    self._attempt(
                        engine,
                        EngineStatus.LOW_SCORE,
                        started,
                        result_count=len(results),
                        best_score=best.score if best is not None else None,
                        reason=outcome_reason,
                    )
                )
                low_pool.extend(results)
            else:
                attempts.append(
                    self._attempt(
                        engine,
                        EngineStatus.NO_RESULT,
                        started,
                        result_count=0,
                        reason=outcome_reason,
                    )
                )
            self._log_attempt(image_ref, attempts[-1])

        low_pool.sort(key=lambda result: result.score, reverse=True)
        return SearchReport(
            mode=normalized_mode,
            threshold=active_threshold,
            attempts=attempts,
            hit=None,
            low_results=low_pool[: self._settings.max_low_results],
        )

    def _resolve_order(self, mode: str, engines: Optional[Sequence[str]]) -> Tuple[str, ...]:
        """解析本次引擎顺序并校验。"""

        if engines is None:
            return self._settings.engines_for_mode(mode)

        order = tuple(str(engine or "").strip().lower() for engine in engines)
        if not order:
            raise ValueError("引擎顺序不能为空")
        if len(set(order)) != len(order):
            raise ValueError("引擎顺序存在重复")
        unknown = [engine for engine in order if engine not in ALLOWED_ENGINES]
        if unknown:
            raise ValueError(f"引擎顺序包含未知引擎: {', '.join(unknown)}")
        return order

    def _attempt(
        self,
        engine: str,
        status: str,
        started: float,
        result_count: int = 0,
        best_score: Optional[float] = None,
        reason: str = "",
    ) -> EngineAttempt:
        """构造一次引擎尝试记录。"""

        elapsed_ms = int((time.monotonic() - started) * 1000)
        return EngineAttempt(
            engine=engine,
            status=status,
            elapsed_ms=elapsed_ms,
            result_count=result_count,
            best_score=best_score,
            reason=reason,
        )

    def _safe(self, value: object) -> str:
        """对错误摘要做密钥脱敏。"""

        return safe_reason(value, self._secrets)

    def _sanitize_results(self, results: Iterable[SearchResult]) -> List[SearchResult]:
        """对第三方返回的展示字段再次脱敏，避免凭据被原样带回。"""

        sanitized: List[SearchResult] = []
        for result in results:
            sanitized.append(
                replace(
                    result,
                    title=redact_text(result.title, self._secrets),
                    author=redact_text(result.author, self._secrets),
                    source_url=redact_text(result.source_url, self._secrets),
                    detail=redact_text(result.detail, self._secrets),
                )
            )
        return sanitized

    def _log_attempt(self, image_ref: str, attempt: EngineAttempt) -> None:
        """记录脱敏后的引擎尝试日志。"""

        if attempt.status in {EngineStatus.HIT, EngineStatus.LOW_SCORE, EngineStatus.NO_RESULT}:
            self._logger.info(
                "图片反查引擎结束: image=%s engine=%s status=%s elapsed_ms=%d results=%d best=%s",
                image_ref,
                attempt.engine,
                attempt.status,
                attempt.elapsed_ms,
                attempt.result_count,
                "None" if attempt.best_score is None else f"{attempt.best_score:.1f}",
            )
            return
        self._logger.warning(
            "图片反查引擎失败: image=%s engine=%s status=%s elapsed_ms=%d reason=%s",
            image_ref,
            attempt.engine,
            attempt.status,
            attempt.elapsed_ms,
            attempt.reason,
        )

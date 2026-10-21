"""核心调度、降级、格式化与脱敏测试。"""

from typing import Dict, List, Tuple

import asyncio
import logging

import pytest

from plugins.image_reverse_search.core import (
    MODE_ANIME,
    MODE_ILLUSTRATION,
    EngineRateLimited,
    EngineRequestError,
    EngineSearchOutcome,
    EngineStatus,
    EngineTimeout,
    ImageInput,
    ImageSearchService,
    SearchReport,
    SearchResult,
    SearchSettings,
    format_report,
    parse_search_command,
)


class FakeBackend:
    """按引擎返回预设结果，并记录实际调用顺序。"""

    def __init__(self, outcomes: Dict[str, object]) -> None:
        self._outcomes = outcomes
        self.calls: List[Tuple[str, float]] = []

    async def search(self, engine: str, image: ImageInput, timeout: float) -> List[SearchResult]:
        del image
        self.calls.append((engine, timeout))
        outcome = self._outcomes[engine]
        if callable(outcome):
            return await outcome()
        if isinstance(outcome, (Exception, EngineSearchOutcome)):
            if isinstance(outcome, Exception):
                raise outcome
            return outcome
        return list(outcome)


def make_image() -> ImageInput:
    """构造测试图片。"""

    return ImageInput.from_bytes(b"fake-image", max_bytes=1024)


_UNSET = object()


def make_result(
    engine: str,
    score: float,
    title: str = "测试标题",
    author: str = "测试作者",
    url: str = "https://example.com/source",
    similarity: object = _UNSET,
) -> SearchResult:
    """构造统一搜索结果。"""

    return SearchResult(
        engine=engine,
        title=title,
        author=author,
        source_url=url,
        similarity=score if similarity is _UNSET else similarity,  # type: ignore[arg-type]
        score=score,
    )


@pytest.mark.asyncio
async def test_illustration_order_stops_at_first_hit() -> None:
    backend = FakeBackend({"saucenao": [make_result("saucenao", 96.0)]})
    service = ImageSearchService(backend, settings=SearchSettings())

    report = await service.search(make_image(), mode=MODE_ILLUSTRATION)

    assert backend.calls == [("saucenao", 15.0)]
    assert report.hit is not None
    assert report.hit.engine == "saucenao"
    assert report.hit_engine == "saucenao"
    assert [attempt.status for attempt in report.attempts] == [EngineStatus.HIT]


@pytest.mark.asyncio
async def test_missing_saucenao_key_falls_back_to_ascii2d() -> None:
    backend = FakeBackend(
        {
            "saucenao": EngineSearchOutcome(skipped=True, reason="未配置 SauceNAO API key"),
            "ascii2d": [make_result("ascii2d", 88.0, similarity=None)],
        }
    )
    service = ImageSearchService(backend, settings=SearchSettings())

    report = await service.search(make_image(), mode=MODE_ILLUSTRATION)

    assert [engine for engine, _timeout in backend.calls] == ["saucenao", "ascii2d"]
    assert report.hit is not None
    assert report.hit.engine == "ascii2d"
    assert report.hit.similarity is None
    assert report.attempts[0].status == EngineStatus.SKIPPED
    assert report.attempts[1].status == EngineStatus.HIT
    assert "SauceNAO" in report.attempts[0].reason
    assert "缺 key 跳过" in format_report(report)


@pytest.mark.asyncio
async def test_partial_engine_failure_keeps_working_results_and_reason() -> None:
    backend = FakeBackend(
        {
            "saucenao": EngineSearchOutcome(
                results=[make_result("saucenao", 94.0)],
                reason="bovw 模式不可用（EngineTimeout），已采用其余模式结果",
            )
        }
    )
    service = ImageSearchService(backend, settings=SearchSettings())

    report = await service.search(make_image(), mode=MODE_ILLUSTRATION)

    assert report.hit is not None
    assert "bovw 模式不可用" in report.attempts[0].reason
    assert "bovw 模式不可用" in format_report(report)


@pytest.mark.asyncio
async def test_engine_timeout_falls_back_without_raising() -> None:
    async def slow_call() -> List[SearchResult]:
        await asyncio.sleep(0.2)
        return []

    backend = FakeBackend({"saucenao": slow_call, "ascii2d": [make_result("ascii2d", 90.0)]})
    service = ImageSearchService(backend, settings=SearchSettings(engine_timeout=0.02))

    report = await service.search(make_image(), mode=MODE_ILLUSTRATION)

    assert [engine for engine, _timeout in backend.calls] == ["saucenao", "ascii2d"]
    assert report.hit is not None
    assert report.hit.engine == "ascii2d"
    assert report.attempts[0].status == EngineStatus.TIMEOUT
    assert "超时" in format_report(report)


@pytest.mark.asyncio
async def test_anime_mode_uses_tracemoe_first() -> None:
    backend = FakeBackend({"tracemoe": [make_result("tracemoe", 92.5)]})
    service = ImageSearchService(backend, settings=SearchSettings())

    report = await service.search(make_image(), mode=MODE_ANIME)

    assert backend.calls == [("tracemoe", 15.0)]
    assert report.hit is not None
    assert report.hit.engine == "tracemoe"


@pytest.mark.asyncio
async def test_rate_limited_engine_falls_back_to_next() -> None:
    backend = FakeBackend(
        {
            "saucenao": EngineRateLimited("HTTP 429 限流"),
            "ascii2d": [make_result("ascii2d", 88.0)],
        }
    )
    service = ImageSearchService(backend, settings=SearchSettings())

    report = await service.search(make_image(), mode=MODE_ILLUSTRATION)

    assert [engine for engine, _timeout in backend.calls] == ["saucenao", "ascii2d"]
    assert report.hit is not None and report.hit.engine == "ascii2d"
    assert report.attempts[0].status == EngineStatus.RATE_LIMITED
    assert "限流" in format_report(report)


@pytest.mark.asyncio
async def test_low_score_results_are_aggregated_instead_of_empty_reply() -> None:
    backend = FakeBackend(
        {
            "saucenao": [make_result("saucenao", 62.0, title="低分A")],
            "ascii2d": EngineRequestError("HTTP 503"),
            "iqdb": [make_result("iqdb", 55.0, title="低分B")],
        }
    )
    service = ImageSearchService(backend, settings=SearchSettings(hit_threshold=80.0))

    report = await service.search(make_image(), mode=MODE_ILLUSTRATION)

    assert report.hit is None
    assert [result.title for result in report.low_results] == ["低分A", "低分B"]
    assert [attempt.status for attempt in report.attempts] == [
        EngineStatus.LOW_SCORE,
        EngineStatus.ERROR,
        EngineStatus.LOW_SCORE,
    ]
    text = format_report(report)
    assert "低分候选" in text
    assert "低分A" in text
    assert "低分B" in text


@pytest.mark.asyncio
async def test_all_engines_no_result_reports_every_status() -> None:
    backend = FakeBackend(
        {
            "saucenao": [],
            "ascii2d": EngineTimeout("请求超时"),
            "iqdb": EngineRateLimited("HTTP 429 限流"),
        }
    )
    service = ImageSearchService(
        backend,
        settings=SearchSettings(illustration_engines=("saucenao", "ascii2d", "iqdb")),
    )

    report = await service.search(make_image(), mode=MODE_ILLUSTRATION)

    assert report.hit is None
    assert report.low_results == []
    assert [attempt.status for attempt in report.attempts] == [
        EngineStatus.NO_RESULT,
        EngineStatus.TIMEOUT,
        EngineStatus.RATE_LIMITED,
    ]
    text = format_report(report)
    assert "所有引擎均未命中" in text
    assert "SauceNAO：无结果" in text
    assert "ascii2d：超时" in text
    assert "IQDB：限流" in text


@pytest.mark.asyncio
async def test_engine_order_is_configurable() -> None:
    backend = FakeBackend({"iqdb": [make_result("iqdb", 95.0)]})
    service = ImageSearchService(
        backend,
        settings=SearchSettings(illustration_engines=("iqdb", "saucenao")),
    )

    report = await service.search(make_image())

    assert backend.calls == [("iqdb", 15.0)]
    assert report.hit is not None
    assert report.hit.engine == "iqdb"


@pytest.mark.asyncio
async def test_per_engine_timeout_override() -> None:
    backend = FakeBackend({"saucenao": [make_result("saucenao", 99.0)]})
    service = ImageSearchService(
        backend,
        settings=SearchSettings(engine_timeouts={"saucenao": 3.5}),
    )

    await service.search(make_image())

    assert backend.calls == [("saucenao", 3.5)]


def test_format_hit_contains_required_fields() -> None:
    report = SearchReport(
        mode=MODE_ILLUSTRATION,
        threshold=80.0,
        attempts=[],
        hit=make_result("saucenao", 96.4, title="作品名", author="画师名", url="https://example.com/a"),
    )

    text = format_report(report)

    assert "命中引擎：SauceNAO" in text
    assert "标题：作品名" in text
    assert "作者/画师：画师名" in text
    assert "来源链接：https://example.com/a" in text
    assert "相似度：96.4%" in text


@pytest.mark.asyncio
async def test_secret_is_redacted_from_reason_and_logs(caplog: pytest.LogCaptureFixture) -> None:
    secret = "super-secret-api-key-1234567890"
    backend = FakeBackend(
        {
            "saucenao": EngineRequestError(f"request failed api_key={secret}"),
            "ascii2d": [make_result("ascii2d", 90.0, title=f"泄露的标题 {secret}")],
        }
    )
    service = ImageSearchService(backend, settings=SearchSettings(), secrets=[secret])

    with caplog.at_level(logging.DEBUG):
        report = await service.search(make_image())

    assert report.hit is not None
    assert secret not in report.hit.title
    assert secret not in format_report(report)
    assert secret not in caplog.text
    assert "[REDACTED]" in report.attempts[0].reason


def test_search_settings_reject_unknown_engine() -> None:
    with pytest.raises(ValueError):
        SearchSettings(illustration_engines=("unknown",))


def test_parse_search_command() -> None:
    assert parse_search_command("/搜图") == MODE_ILLUSTRATION
    assert parse_search_command("/搜番") == MODE_ANIME
    assert parse_search_command("/搜图 番剧") == MODE_ANIME
    assert parse_search_command("/搜图 插画") == MODE_ILLUSTRATION
    assert parse_search_command("搜番") == MODE_ANIME
    assert parse_search_command("随便聊聊") is None
    assert parse_search_command("我想搜图") is None

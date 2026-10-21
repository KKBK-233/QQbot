"""PicImageSearch 结果归一化与密钥解析测试。"""

from types import SimpleNamespace

import asyncio

import pytest

from plugins.image_reverse_search.core import (
    ENGINE_ASCII2D,
    ENGINE_IQDB,
    ENGINE_SAUCENAO,
    ENGINE_TRACEMOE,
    resolve_api_key,
)
from plugins.image_reverse_search.core.backend import (
    normalize_ascii2d_response,
    normalize_iqdb_response,
    normalize_saucenao_response,
    normalize_tracemoe_response,
)


def test_normalize_saucenao_keeps_title_author_url_similarity() -> None:
    response = SimpleNamespace(
        url="https://saucenao.com/search.php?url=abc",
        raw=[
            SimpleNamespace(
                similarity=95.5,
                title="作品",
                author="画师",
                url="https://www.pixiv.net/artworks/1",
                ext_urls=[],
                index_name="Pixiv",
                thumbnail="https://img.example/thumb.jpg",
            )
        ],
    )

    results = normalize_saucenao_response(response)

    assert len(results) == 1
    result = results[0]
    assert result.engine == ENGINE_SAUCENAO
    assert result.title == "作品"
    assert result.author == "画师"
    assert result.source_url == "https://www.pixiv.net/artworks/1"
    assert result.similarity == 95.5


def test_normalize_ascii2d_has_no_numeric_similarity() -> None:
    response = SimpleNamespace(
        url="https://ascii2d.net/search/file",
        raw=[
            SimpleNamespace(
                title="",
                author="",
                url="",
                url_list=[
                    SimpleNamespace(href="https://www.pixiv.net/artworks/22", text="Pixiv 作品"),
                ],
                detail="1000x1000",
                thumbnail="https://ascii2d.net/thumb.jpg",
            )
        ],
    )

    results = normalize_ascii2d_response(response, implicit_score=84.0)

    assert len(results) == 1
    result = results[0]
    assert result.engine == ENGINE_ASCII2D
    assert result.title == "Pixiv 作品"
    assert result.source_url == "https://www.pixiv.net/artworks/22"
    assert result.similarity is None
    assert result.score == 84.0
    assert "未提供评分" in result.similarity_label


def test_normalize_iqdb_includes_low_score_tail() -> None:
    response = SimpleNamespace(
        url="https://iqdb.org/?url=abc",
        raw=[
            SimpleNamespace(
                content="Best match",
                source="Pixiv",
                other_source=[],
                url="https://example.com/best",
                similarity=91.0,
                size="1000x1000",
                thumbnail="https://iqdb.org/thumb.jpg",
            )
        ],
        more=[
            SimpleNamespace(
                content="Additional match",
                source="Danbooru",
                other_source=[{"source": "Danbooru", "url": "https://example.com/more"}],
                url="",
                similarity=70.0,
                size="800x800",
                thumbnail="",
            )
        ],
    )

    results = normalize_iqdb_response(response)

    assert [result.engine for result in results] == [ENGINE_IQDB, ENGINE_IQDB]
    assert results[0].title == "Best match"
    assert results[0].similarity == 91.0
    assert results[1].source_url == "https://example.com/more"


def test_normalize_tracemoe_builds_anime_title_and_scene() -> None:
    response = SimpleNamespace(
        url="https://trace.moe/?url=abc",
        raw=[
            SimpleNamespace(
                similarity=93.25,
                title_chinese="测试番剧",
                title_romaji="Test Anime",
                title_native="テスト",
                title_english="Test",
                episode=3,
                anilist_id=123,
                From=65.0,
                To=72.0,
                image="https://trace.moe/thumb.jpg",
            )
        ],
    )

    results = normalize_tracemoe_response(response)

    assert len(results) == 1
    result = results[0]
    assert result.engine == ENGINE_TRACEMOE
    assert result.title == "测试番剧 第3集"
    assert result.source_url == "https://anilist.co/anime/123"
    assert result.similarity == 93.25
    assert result.detail == "01:05 - 01:12"


def test_normalize_rejects_non_http_source_url() -> None:
    response = SimpleNamespace(
        url="",
        raw=[
            SimpleNamespace(
                similarity=90.0,
                title="x",
                author="y",
                url="javascript:alert(1)",
                ext_urls=[],
            )
        ],
    )

    results = normalize_saucenao_response(response)

    assert results[0].source_url == ""


def test_backend_options_repr_hides_keys() -> None:
    from plugins.image_reverse_search.core import BackendOptions

    options = BackendOptions(sauce_api_key="sauce-secret", trace_api_key="trace-secret")
    text = repr(options)
    assert "sauce-secret" not in text
    assert "trace-secret" not in text


def test_resolve_api_key_prefers_config_then_environment() -> None:
    assert resolve_api_key("config-key", "SAUCENAO_API_KEY", {"SAUCENAO_API_KEY": "env-key"}) == "config-key"
    assert resolve_api_key("", "SAUCENAO_API_KEY", {"SAUCENAO_API_KEY": "env-key"}) == "env-key"
    assert resolve_api_key("", "SAUCENAO_API_KEY", {}) == ""


def test_normalize_saucenao_sorts_by_similarity() -> None:
    def item(similarity: float, title: str) -> SimpleNamespace:
        return SimpleNamespace(
            similarity=similarity,
            title=title,
            author="a",
            url="https://example.com/x",
            ext_urls=[],
            index_name="",
            thumbnail="",
        )

    response = SimpleNamespace(
        url="",
        raw=[item(50.0, "low"), item(90.0, "high")],
    )

    results = normalize_saucenao_response(response)

    assert [result.title for result in results] == ["high", "low"]


class _FakeSauceNao:
    last_kwargs = {}

    def __init__(self, **kwargs):
        _FakeSauceNao.last_kwargs = kwargs

    async def search(self, **kwargs):
        _FakeSauceNao.last_kwargs["search"] = kwargs
        return SimpleNamespace(
            url="https://saucenao.com/search.php",
            raw=[
                SimpleNamespace(
                    similarity=97.0,
                    title="作品",
                    author="画师",
                    url="https://example.com/a",
                    ext_urls=[],
                    index_name="Pixiv",
                    thumbnail="",
                )
            ],
        )


class _FakeNetwork:
    last_kwargs = {}

    def __init__(self, **kwargs):
        _FakeNetwork.last_kwargs = kwargs

    async def __aenter__(self):
        return object()

    async def __aexit__(self, exc_type, exc, tb):
        return None


class _FakeLibrary:
    Network = _FakeNetwork
    SauceNAO = _FakeSauceNao


@pytest.mark.asyncio
async def test_backend_passes_api_key_and_reuses_network(monkeypatch) -> None:
    from plugins.image_reverse_search.core import BackendOptions, ImageInput, PicImageSearchBackend

    backend = PicImageSearchBackend(
        BackendOptions(sauce_api_key="fake-key-for-test", environ={}),
    )
    monkeypatch.setattr(backend, "_load_library", lambda: _FakeLibrary)

    results = await backend.search(
        "saucenao",
        ImageInput.from_bytes(b"image", max_bytes=1024),
        timeout=7.5,
    )

    assert results[0].title == "作品"
    assert _FakeSauceNao.last_kwargs["api_key"] == "fake-key-for-test"
    assert _FakeSauceNao.last_kwargs["numres"] == 10
    assert _FakeSauceNao.last_kwargs["search"]["file"] == b"image"
    assert _FakeNetwork.last_kwargs["timeout"] == 7.5


@pytest.mark.asyncio
async def test_backend_skips_saucenao_without_key(monkeypatch) -> None:
    from plugins.image_reverse_search.core import (
        BackendOptions,
        EngineSearchOutcome,
        ImageInput,
        PicImageSearchBackend,
    )

    backend = PicImageSearchBackend(BackendOptions(sauce_api_key="", environ={}))

    def _must_not_load_library():
        raise AssertionError("缺 key 时不应加载 PicImageSearch 或发起网络请求")

    monkeypatch.setattr(backend, "_load_library", _must_not_load_library)
    outcome = await backend.search(
        "saucenao",
        ImageInput.from_bytes(b"image", max_bytes=1024),
        timeout=1.0,
    )

    assert isinstance(outcome, EngineSearchOutcome)
    assert outcome.skipped is True
    assert "未配置 SauceNAO API key" in outcome.reason


class _FakeAscii2D:
    modes = []
    fail_modes = set()
    delays = {}

    def __init__(self, **kwargs):
        self.bovw = bool(kwargs.get("bovw"))
        self.client = kwargs.get("client")

    async def search(self, **kwargs):
        _FakeAscii2D.modes.append(self.bovw)
        if delay := _FakeAscii2D.delays.get(self.bovw):
            await asyncio.sleep(delay)
        if self.bovw in _FakeAscii2D.fail_modes:
            raise RuntimeError("mode failed")
        title = "bovw 结果" if self.bovw else "色街结果"
        url = "https://example.com/bovw" if self.bovw else "https://example.com/color"
        return SimpleNamespace(
            url="https://ascii2d.net/search/file",
            raw=[
                SimpleNamespace(
                    title=title,
                    author="",
                    url="",
                    url_list=[SimpleNamespace(href=url, text=title)],
                    detail="1000x1000",
                    thumbnail="",
                )
            ],
        )


class _FakeAscii2DLibrary:
    Network = _FakeNetwork
    Ascii2D = _FakeAscii2D


@pytest.mark.asyncio
async def test_backend_ascii2d_queries_color_and_bovw(monkeypatch) -> None:
    from plugins.image_reverse_search.core import BackendOptions, ImageInput, PicImageSearchBackend

    _FakeAscii2D.modes = []
    _FakeAscii2D.fail_modes = set()
    _FakeAscii2D.delays = {}
    backend = PicImageSearchBackend(BackendOptions(environ={}))
    monkeypatch.setattr(backend, "_load_library", lambda: _FakeAscii2DLibrary)

    results = await backend.search(
        "ascii2d",
        ImageInput.from_bytes(b"image", max_bytes=1024),
        timeout=5.0,
    )

    assert sorted(_FakeAscii2D.modes) == [False, True]
    assert {result.title for result in results} == {"色街结果", "bovw 结果"}
    assert {result.detail.split(" · ")[0] for result in results} == {"色街", "bovw"}


@pytest.mark.asyncio
async def test_backend_ascii2d_keeps_working_mode_when_other_fails(monkeypatch) -> None:
    from plugins.image_reverse_search.core import BackendOptions, ImageInput, PicImageSearchBackend

    _FakeAscii2D.modes = []
    _FakeAscii2D.fail_modes = {True}
    _FakeAscii2D.delays = {}
    backend = PicImageSearchBackend(BackendOptions(environ={}))
    monkeypatch.setattr(backend, "_load_library", lambda: _FakeAscii2DLibrary)

    outcome = await backend.search(
        "ascii2d",
        ImageInput.from_bytes(b"image", max_bytes=1024),
        timeout=5.0,
    )
    from plugins.image_reverse_search.core import EngineSearchOutcome

    assert isinstance(outcome, EngineSearchOutcome)
    assert "bovw模式不可用" in outcome.reason
    results = outcome.results

    assert [result.title for result in results] == ["色街结果"]


@pytest.mark.asyncio
async def test_backend_ascii2d_slow_mode_does_not_discard_fast_mode(monkeypatch) -> None:
    from plugins.image_reverse_search.core import BackendOptions, ImageInput, PicImageSearchBackend

    _FakeAscii2D.modes = []
    _FakeAscii2D.fail_modes = set()
    _FakeAscii2D.delays = {True: 0.2}
    backend = PicImageSearchBackend(BackendOptions(environ={}))
    monkeypatch.setattr(backend, "_load_library", lambda: _FakeAscii2DLibrary)

    outcome = await backend.search(
        "ascii2d",
        ImageInput.from_bytes(b"image", max_bytes=1024),
        timeout=0.1,
    )
    from plugins.image_reverse_search.core import EngineSearchOutcome

    assert isinstance(outcome, EngineSearchOutcome)
    results = outcome.results

    assert [result.title for result in results] == ["色街结果"]

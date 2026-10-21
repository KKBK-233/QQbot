"""基于 PicImageSearch 的多引擎后端与结果归一化。"""

from dataclasses import dataclass, field, replace
from typing import Any, List, Mapping, Optional, Sequence

import asyncio
import importlib

from .errors import EngineError, EngineRateLimited, EngineRequestError, EngineTimeout, EngineUnavailable
from .image import ImageInput
from .models import (
    ENGINE_ASCII2D,
    ENGINE_IQDB,
    ENGINE_SAUCENAO,
    ENGINE_TRACEMOE,
    ALLOWED_ENGINES,
    EngineSearchOutcome,
    SearchResult,
)
from .security import resolve_api_key

_MAX_TEXT_LENGTH = 300


@dataclass(frozen=True)
class BackendOptions:
    """PicImageSearch 后端配置；密钥字段不参与 repr。"""

    sauce_api_key: str = field(default="", repr=False)
    sauce_api_key_env: str = "SAUCENAO_API_KEY"
    trace_api_key: str = field(default="", repr=False)
    trace_api_key_env: str = "TRACE_MOE_API_KEY"
    sauce_numres: int = 10
    sauce_hide: int = 0
    sauce_min_similarity: float = 0.0
    ascii2d_implicit_score: float = 85.0
    trace_mute: bool = False
    proxies: str = ""
    verify_ssl: bool = True
    environ: Optional[Mapping[str, str]] = None

    def secrets(self) -> List[str]:
        """返回当前可能使用的密钥列表，仅用于日志脱敏。"""

        values = [
            resolve_api_key(self.sauce_api_key, self.sauce_api_key_env, self.environ),
            resolve_api_key(self.trace_api_key, self.trace_api_key_env, self.environ),
        ]
        return [value for value in values if value]


class PicImageSearchBackend:
    """使用 PicImageSearch 调用 SauceNAO、ascii2d、IQDB 与 trace.moe。"""

    def __init__(self, options: Optional[BackendOptions] = None) -> None:
        """保存配置；PicImageSearch 在实际搜索时才导入。"""

        self.options = options or BackendOptions()

    async def search(
        self,
        engine: str,
        image: ImageInput,
        timeout: float,
    ) -> List[SearchResult] | EngineSearchOutcome:
        """调用单个引擎并返回归一化结果。

        缺少 key 或缺少第三方依赖时返回 ``EngineSearchOutcome(skipped=True)``，
        由调度层标记“跳过”并继续后续无需 key 的引擎，不向调用方抛异常。
        """

        normalized_engine = str(engine or "").strip().lower()
        if normalized_engine not in ALLOWED_ENGINES:
            raise EngineUnavailable(f"未知引擎: {normalized_engine}")
        if normalized_engine == ENGINE_SAUCENAO and not self._sauce_key():
            return EngineSearchOutcome(skipped=True, reason="未配置 SauceNAO API key")

        try:
            library = self._load_library()
            async with library.Network(
                proxies=self.options.proxies or None,
                timeout=timeout,
                verify_ssl=self.options.verify_ssl,
            ) as client:
                if normalized_engine == ENGINE_SAUCENAO:
                    return await self._search_saucenao(library, client, image)
                if normalized_engine == ENGINE_ASCII2D:
                    return await self._search_ascii2d(library, client, image, timeout=timeout)
                if normalized_engine == ENGINE_IQDB:
                    return await self._search_iqdb(library, client, image)
                return await self._search_tracemoe(library, client, image)
        except EngineUnavailable as exc:
            return EngineSearchOutcome(skipped=True, reason=str(exc))
        except (EngineTimeout, EngineRateLimited, EngineRequestError):
            raise
        except Exception as exc:
            raise self._translate_exception(exc) from None

    def _load_library(self) -> Any:
        """延迟导入 PicImageSearch，缺失时给出可读错误。"""

        try:
            return importlib.import_module("PicImageSearch")
        except ImportError as exc:
            raise EngineUnavailable("未安装 PicImageSearch 依赖") from exc

    async def _search_saucenao(self, library: Any, client: Any, image: ImageInput) -> List[SearchResult]:
        """调用 SauceNAO。"""

        api_key = self._sauce_key()
        if not api_key:
            raise EngineUnavailable("未配置 SauceNAO API key")
        searcher = library.SauceNAO(
            api_key=api_key,
            numres=self.options.sauce_numres,
            hide=self.options.sauce_hide,
            minsim=self.options.sauce_min_similarity,
            client=client,
        )
        response = await searcher.search(**image.search_kwargs())
        return normalize_saucenao_response(response)

    async def _search_ascii2d(
        self,
        library: Any,
        client: Any,
        image: ImageInput,
        *,
        timeout: float,
    ) -> List[SearchResult]:
        """同时查询 ascii2d 色街模式与 bovw 特征模式。

        两种模式共享一次引擎超时预算，但各自最多使用其中 80%，避免一个模式
        卡死时丢弃另一个模式已经拿到的结果。任一模式可用就返回去重结果；
        两种模式都失败时才把失败上抛给调度层。
        """

        modes = ((False, "色街"), (True, "bovw"))
        mode_timeout = max(0.01, float(timeout) * 0.8)
        outcomes = await asyncio.gather(
            *(
                asyncio.wait_for(
                    self._search_ascii2d_mode(library, client, image, bovw=bovw, mode_label=label),
                    timeout=mode_timeout,
                )
                for bovw, label in modes
            ),
            return_exceptions=True,
        )
        results: List[SearchResult] = []
        errors: List[tuple[str, Exception]] = []
        for (_bovw, label), outcome in zip(modes, outcomes, strict=True):
            if isinstance(outcome, TimeoutError):
                errors.append((label, EngineTimeout("ascii2d 请求超时")))
                continue
            if isinstance(outcome, Exception):
                errors.append((label, outcome))
                continue
            results.extend(outcome)
        if results:
            deduplicated = self._deduplicate_ascii2d_results(results)
            if not errors:
                return deduplicated
            failed_modes = "、".join(f"{label}模式不可用（{type(error).__name__}）" for label, error in errors)
            return EngineSearchOutcome(
                results=deduplicated,
                reason=f"{failed_modes}，已采用其余模式结果",
            )
        if errors:
            first_error = errors[0][1]
            if isinstance(first_error, EngineError):
                raise first_error
            raise self._translate_exception(first_error)
        return []

    async def _search_ascii2d_mode(
        self,
        library: Any,
        client: Any,
        image: ImageInput,
        *,
        bovw: bool,
        mode_label: str,
    ) -> List[SearchResult]:
        """执行 ascii2d 的单一模式并标注来源模式。"""

        searcher = library.Ascii2D(bovw=bovw, client=client)
        response = await searcher.search(**image.search_kwargs())
        results = normalize_ascii2d_response(response, implicit_score=self.options.ascii2d_implicit_score)
        return [
            replace(
                result,
                detail=f"{mode_label} · {result.detail}" if result.detail else mode_label,
            )
            for result in results
        ]

    @staticmethod
    def _deduplicate_ascii2d_results(results: Sequence[SearchResult]) -> List[SearchResult]:
        """按来源链接/标题去重，保留色街模式优先的结果。"""

        deduplicated: List[SearchResult] = []
        seen: set[tuple[str, str]] = set()
        for result in sorted(results, key=lambda item: item.score, reverse=True):
            key = (result.source_url, result.title)
            if key in seen:
                continue
            seen.add(key)
            deduplicated.append(result)
        return deduplicated

    async def _search_iqdb(self, library: Any, client: Any, image: ImageInput) -> List[SearchResult]:
        """调用 IQDB。"""

        searcher = library.Iqdb(client=client)
        response = await searcher.search(**image.search_kwargs())
        return normalize_iqdb_response(response)

    async def _search_tracemoe(self, library: Any, client: Any, image: ImageInput) -> List[SearchResult]:
        """调用 trace.moe。"""

        api_key = resolve_api_key(
            self.options.trace_api_key,
            self.options.trace_api_key_env,
            self.options.environ,
        )
        searcher = library.TraceMoe(mute=self.options.trace_mute, size=None, client=client)
        response = await searcher.search(key=api_key or None, **image.search_kwargs())
        return normalize_tracemoe_response(response)

    def _sauce_key(self) -> str:
        """解析 SauceNAO key，缺失时返回空字符串。"""

        return resolve_api_key(
            self.options.sauce_api_key,
            self.options.sauce_api_key_env,
            self.options.environ,
        )

    @staticmethod
    def _translate_exception(exc: Exception) -> Exception:
        """把第三方异常转换为不含响应正文的安全错误。"""

        try:
            import httpx
        except ImportError:
            return EngineRequestError(f"请求失败（{type(exc).__name__}）")

        if isinstance(exc, httpx.TimeoutException):
            return EngineTimeout("请求超时")
        response_status = getattr(getattr(exc, "response", None), "status_code", None)
        if response_status == 429:
            return EngineRateLimited("HTTP 429 限流")
        if isinstance(exc, httpx.HTTPStatusError):
            return EngineRequestError(f"HTTP {exc.response.status_code}")
        if isinstance(exc, httpx.RequestError):
            return EngineRequestError(f"网络请求失败（{type(exc).__name__}）")
        if type(exc).__name__ == "ParsingError":
            return EngineRequestError("响应解析失败")
        return EngineRequestError(f"请求失败（{type(exc).__name__}）")


def normalize_saucenao_response(response: Any) -> List[SearchResult]:
    """把 SauceNAOResponse 归一化为统一结果。"""

    fallback_url = _safe_url(getattr(response, "url", ""))
    results: List[SearchResult] = []
    for item in list(getattr(response, "raw", []) or []):
        similarity = _to_float(getattr(item, "similarity", None))
        if similarity is None:
            continue
        title = _clean_text(getattr(item, "title", "")) or _clean_text(getattr(item, "index_name", ""))
        author = _clean_text(getattr(item, "author", "")) or "未知"
        source_url = _first_safe_url(
            getattr(item, "url", ""),
            *_as_list(getattr(item, "ext_urls", [])),
            fallback_url,
        )
        results.append(
            SearchResult(
                engine=ENGINE_SAUCENAO,
                title=title or "（SauceNAO 未提供标题）",
                author=author,
                source_url=source_url,
                similarity=similarity,
                score=similarity,
                detail=_clean_text(getattr(item, "index_name", "")),
                thumbnail=_safe_url(getattr(item, "thumbnail", "")),
            )
        )
    return _sort_results(results)


def normalize_ascii2d_response(response: Any, implicit_score: float = 85.0) -> List[SearchResult]:
    """把 Ascii2DResponse 归一化为统一结果。

    ascii2d 不提供相似度字段，因此 ``similarity`` 保持为空，仅使用配置的
    隐式分数参与命中判断；展示时会明确标注“未提供评分”。
    """

    fallback_url = _safe_url(getattr(response, "url", ""))
    results: List[SearchResult] = []
    for item in list(getattr(response, "raw", []) or []):
        url_list = list(getattr(item, "url_list", []) or [])
        link_texts = [_clean_text(getattr(link, "text", "")) for link in url_list]
        link_urls = [_safe_url(getattr(link, "href", "")) for link in url_list]
        title = _clean_text(getattr(item, "title", "")) or next((text for text in link_texts if text), "")
        source_url = _first_safe_url(getattr(item, "url", ""), *link_urls, fallback_url)
        if not title and not source_url:
            continue
        results.append(
            SearchResult(
                engine=ENGINE_ASCII2D,
                title=title or "（ascii2d 未提供标题）",
                author=_clean_text(getattr(item, "author", "")) or "未知",
                source_url=source_url,
                similarity=None,
                score=implicit_score,
                detail=_clean_text(getattr(item, "detail", "")),
                thumbnail=_safe_url(getattr(item, "thumbnail", "")),
            )
        )
    return _sort_results(results)


def normalize_iqdb_response(response: Any) -> List[SearchResult]:
    """把 IqdbResponse 归一化为统一结果，包含低分补充结果。"""

    fallback_url = _safe_url(getattr(response, "url", ""))
    items = list(getattr(response, "raw", []) or []) + list(getattr(response, "more", []) or [])
    results: List[SearchResult] = []
    for item in items:
        similarity = _to_float(getattr(item, "similarity", None))
        if similarity is None:
            continue
        title = _clean_text(getattr(item, "content", "")) or _clean_text(getattr(item, "source", ""))
        other_sources = list(getattr(item, "other_source", []) or [])
        other_urls = [_safe_url(source.get("url", "")) for source in other_sources if isinstance(source, Mapping)]
        source_url = _first_safe_url(getattr(item, "url", ""), *other_urls, fallback_url)
        results.append(
            SearchResult(
                engine=ENGINE_IQDB,
                title=title or "（IQDB 未提供标题）",
                author="未知",
                source_url=source_url,
                similarity=similarity,
                score=similarity,
                detail=_clean_text(getattr(item, "size", "")),
                thumbnail=_safe_url(getattr(item, "thumbnail", "")),
            )
        )
    return _sort_results(results)


def normalize_tracemoe_response(response: Any) -> List[SearchResult]:
    """把 TraceMoeResponse 归一化为统一结果。"""

    fallback_url = _safe_url(getattr(response, "url", ""))
    results: List[SearchResult] = []
    for item in list(getattr(response, "raw", []) or []):
        similarity = _to_float(getattr(item, "similarity", None))
        if similarity is None:
            continue
        title = _first_text(
            getattr(item, "title_chinese", ""),
            getattr(item, "title_romaji", ""),
            getattr(item, "title_native", ""),
            getattr(item, "title_english", ""),
        )
        episode = getattr(item, "episode", None)
        if isinstance(episode, int) and episode > 0:
            title = f"{title or '（trace.moe 未提供标题）'} 第{episode}集"
        anilist_id = getattr(item, "anilist_id", 0)
        anilist_url = f"https://anilist.co/anime/{anilist_id}" if anilist_id else ""
        source_url = _first_safe_url(anilist_url, fallback_url)
        results.append(
            SearchResult(
                engine=ENGINE_TRACEMOE,
                title=title or "（trace.moe 未提供标题）",
                author="未知",
                source_url=source_url,
                similarity=similarity,
                score=similarity,
                detail=_format_scene_detail(item),
                thumbnail=_safe_url(getattr(item, "image", "")),
            )
        )
    return _sort_results(results)


def _format_scene_detail(item: Any) -> str:
    """格式化番剧场景时间信息。"""

    from_value = _to_float(getattr(item, "From", None))
    to_value = _to_float(getattr(item, "To", None))
    if from_value is None or to_value is None:
        return ""
    return f"{_format_seconds(from_value)} - {_format_seconds(to_value)}"


def _format_seconds(value: float) -> str:
    """把秒数格式化为 mm:ss。"""

    total = max(0, int(value))
    minutes, seconds = divmod(total, 60)
    return f"{minutes:02d}:{seconds:02d}"


def _sort_results(results: Sequence[SearchResult]) -> List[SearchResult]:
    """按有效分数从高到低排序。"""

    return sorted(results, key=lambda result: result.score, reverse=True)


def _clean_text(value: object) -> str:
    """压平外部文本并限制长度，避免换行破坏回复结构。"""

    text = " ".join(str(value or "").split())
    if len(text) > _MAX_TEXT_LENGTH:
        text = text[: _MAX_TEXT_LENGTH - 1] + "…"
    return text


def _first_text(*values: object) -> str:
    """返回第一个非空文本。"""

    for value in values:
        text = _clean_text(value)
        if text:
            return text
    return ""


def _safe_url(value: object) -> str:
    """只接受 HTTP(S) URL，拒绝 javascript: 等外部协议。"""

    text = str(value or "").strip()
    if text.startswith(("http://", "https://")):
        return text
    return ""


def _first_safe_url(*values: object) -> str:
    """返回第一个安全的 HTTP(S) URL。"""

    for value in values:
        url = _safe_url(value)
        if url:
            return url
    return ""


def _as_list(value: object) -> List[object]:
    """把外部值转换为列表。"""

    if isinstance(value, list):
        return value
    if value is None:
        return []
    return [value]


def _to_float(value: object) -> Optional[float]:
    """把外部相似度值转换为 float，非法值返回 None。"""

    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    if number != number:
        return None
    return number

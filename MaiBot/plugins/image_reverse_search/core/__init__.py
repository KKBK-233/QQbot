"""图片反查核心模块，不依赖 MaiBot 插件 SDK。"""

from .backend import BackendOptions, PicImageSearchBackend
from .command import parse_search_command
from .errors import EngineRateLimited, EngineRequestError, EngineTimeout, EngineUnavailable
from .formatter import format_report, format_usage
from .image import ImageInput
from .message import (
    extract_image,
    extract_message_text,
    extract_reply_target_id,
    is_group_message,
    resolve_stream_id,
)
from .models import (
    ALLOWED_ENGINES,
    DEFAULT_ANIME_ENGINES,
    DEFAULT_ILLUSTRATION_ENGINES,
    ENGINE_ASCII2D,
    ENGINE_IQDB,
    ENGINE_LABELS,
    ENGINE_SAUCENAO,
    ENGINE_TRACEMOE,
    MODE_ANIME,
    MODE_ILLUSTRATION,
    EngineAttempt,
    EngineSearchOutcome,
    EngineStatus,
    SearchReport,
    SearchResult,
    SearchSettings,
)
from .security import SecretRedactingFilter, mask_url, redact_text, resolve_api_key, safe_reason
from .service import ImageSearchService, SearchBackend

__all__ = [
    "ALLOWED_ENGINES",
    "BackendOptions",
    "DEFAULT_ANIME_ENGINES",
    "DEFAULT_ILLUSTRATION_ENGINES",
    "ENGINE_ASCII2D",
    "ENGINE_IQDB",
    "ENGINE_LABELS",
    "ENGINE_SAUCENAO",
    "ENGINE_TRACEMOE",
    "EngineAttempt",
    "EngineRateLimited",
    "EngineRequestError",
    "EngineSearchOutcome",
    "EngineStatus",
    "EngineTimeout",
    "EngineUnavailable",
    "extract_image",
    "extract_message_text",
    "extract_reply_target_id",
    "ImageInput",
    "ImageSearchService",
    "is_group_message",
    "MODE_ANIME",
    "MODE_ILLUSTRATION",
    "PicImageSearchBackend",
    "SearchBackend",
    "SearchReport",
    "SearchResult",
    "SecretRedactingFilter",
    "SearchSettings",
    "format_report",
    "format_usage",
    "mask_url",
    "parse_search_command",
    "redact_text",
    "resolve_stream_id",
    "resolve_api_key",
    "safe_reason",
]

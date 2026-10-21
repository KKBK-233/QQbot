"""消息解析与图片输入测试。"""

import base64
import logging

import pytest

from plugins.image_reverse_search.core.message import (
    extract_image,
    extract_message_text,
    is_group_message,
    resolve_stream_id,
)
from plugins.image_reverse_search.core.security import SecretRedactingFilter, mask_url, redact_text


def test_extract_message_text_prefers_processed_text() -> None:
    message = {
        "processed_plain_text": "/搜图 番剧",
        "raw_message": [{"type": "text", "data": "ignored"}],
    }
    assert extract_message_text(message) == "/搜图 番剧"


def test_extract_image_prefers_base64() -> None:
    encoded = base64.b64encode(b"image-bytes").decode("ascii")
    message = {
        "raw_message": [
            {"type": "text", "data": "/搜图"},
            {"type": "image", "data": "https://example.com/a.jpg", "binary_data_base64": encoded},
        ]
    }

    image = extract_image(message, max_bytes=1024)

    assert image is not None
    assert image.data == b"image-bytes"


def test_extract_image_from_url() -> None:
    image = extract_image({"raw_message": [{"type": "image", "data": "https://example.com/a.jpg"}]}, 1024)
    assert image is not None
    assert image.url == "https://example.com/a.jpg"


def test_extract_image_from_forward_node() -> None:
    message = {
        "raw_message": [
            {
                "type": "forward",
                "data": [
                    {
                        "segments": [
                            {"type": "image", "data": "https://example.com/forward.jpg"},
                        ]
                    }
                ],
            }
        ]
    }
    image = extract_image(message, 1024)
    assert image is not None
    assert image.url == "https://example.com/forward.jpg"


def test_extract_image_returns_none_without_image() -> None:
    assert extract_image({"raw_message": [{"type": "text", "data": "hi"}]}, 1024) is None


def test_group_and_stream_helpers() -> None:
    message = {
        "session_id": "stream-1",
        "message_info": {"group_info": {"group_id": "123"}},
    }
    assert is_group_message(message) is True
    assert resolve_stream_id(message) == "stream-1"
    assert is_group_message({"message_info": {"group_info": None}}) is False


def test_image_size_limit() -> None:
    with pytest.raises(ValueError):
        extract_image(
            {"raw_message": [{"type": "image", "binary_data_base64": "YWJjZA=="}]},
            max_bytes=2,
        )


def test_mask_url_removes_query() -> None:
    masked = mask_url("https://user:pass@example.com/path?token=secret&x=1")
    assert masked == "https://example.com/path?[REDACTED]"
    assert "secret" not in masked
    assert "user:pass" not in masked


def test_redact_text_replaces_known_secret_and_token_param() -> None:
    text = redact_text("key=super-secret token=abc123", ["super-secret"])
    assert "super-secret" not in text
    assert "abc123" not in text


def test_http_log_filter_redacts_api_key_and_url_query(caplog: pytest.LogCaptureFixture) -> None:
    secret = "unit-test-secret-value"
    httpx_logger = logging.getLogger("httpx")
    log_filter = SecretRedactingFilter([secret])
    httpx_logger.addFilter(log_filter)
    try:
        with caplog.at_level(logging.INFO, logger="httpx"):
            httpx_logger.info(
                "HTTP Request: POST https://saucenao.com/search.php?api_key=%s&url=https://example.com/a.jpg",
                secret,
            )
    finally:
        httpx_logger.removeFilter(log_filter)

    assert secret not in caplog.text
    assert "example.com/a.jpg" not in caplog.text
    assert "search.php?[REDACTED]" in caplog.text

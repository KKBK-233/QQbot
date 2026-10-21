"""从插件事件消息中提取文本、图片与聊天流信息。"""

from collections.abc import Iterator, Mapping
from typing import Any, List, Optional

from .image import ImageInput

_IMAGE_TYPES = {"image"}


def extract_message_text(message: Mapping[str, Any]) -> str:
    """提取消息纯文本。"""

    for key in ("processed_plain_text", "plain_text"):
        value = message.get(key)
        if isinstance(value, str) and value.strip():
            return value

    texts: List[str] = []
    for segment in _iter_segments(_message_segments(message)):
        if segment.get("type") != "text":
            continue
        value = segment.get("data")
        if isinstance(value, str) and value:
            texts.append(value)
    return " ".join(texts)


def extract_image(message: Mapping[str, Any], max_bytes: int) -> Optional[ImageInput]:
    """从消息段中提取第一张可用的图片。

    Args:
        message: 插件事件中的消息字典。
        max_bytes: 允许的最大图片字节数。

    Returns:
        Optional[ImageInput]: 解析成功时返回图片输入；消息中没有图片时返回 ``None``。

    Raises:
        ValueError: 找到图片段但图片数据非法时抛出。
    """

    for segment in _iter_segments(_message_segments(message)):
        if segment.get("type") not in _IMAGE_TYPES:
            continue
        image = _image_from_segment(segment, max_bytes)
        if image is not None:
            return image
    return None


def extract_reply_target_id(message: Mapping[str, Any]) -> Optional[str]:
    """提取当前消息引用的目标消息 ID。"""

    direct = message.get("reply_to")
    if isinstance(direct, str) and direct.strip():
        return direct.strip()

    for segment in _iter_segments(_message_segments(message)):
        if segment.get("type") != "reply":
            continue
        data = segment.get("data")
        if isinstance(data, Mapping):
            target = data.get("target_message_id")
        else:
            target = data
        if isinstance(target, str) and target.strip():
            return target.strip()
    return None


def is_group_message(message: Mapping[str, Any]) -> bool:
    """判断消息是否来自群聊。"""

    message_info = message.get("message_info")
    if not isinstance(message_info, Mapping):
        return False
    group_info = message_info.get("group_info")
    return isinstance(group_info, Mapping) and bool(group_info.get("group_id"))


def resolve_stream_id(message: Mapping[str, Any], fallback: str = "") -> str:
    """从消息中解析发送回复使用的聊天流 ID。"""

    for key in ("session_id", "stream_id"):
        value = message.get(key)
        if isinstance(value, str) and value:
            return value
    return fallback


def _image_from_segment(segment: Mapping[str, Any], max_bytes: int) -> Optional[ImageInput]:
    """把单个图片段转换为 ImageInput。"""

    encoded = segment.get("binary_data_base64") or segment.get("base64")
    if isinstance(encoded, str) and encoded.strip():
        return ImageInput.from_base64(encoded, max_bytes)

    data = segment.get("data")
    if isinstance(data, Mapping):
        data = data.get("url") or data.get("path") or data.get("data")

    text = str(data or "").strip()
    if not text:
        return None
    if text.startswith(("http://", "https://")):
        return ImageInput.from_url(text)
    if text.startswith(("data:image/", "base64://")):
        return ImageInput.from_base64(text, max_bytes)

    # 图片段可能只带 hash 而没有可读取内容，此时跳过而不是把 hash 当 URL。
    if len(text) == 64 and all(character in "0123456789abcdefABCDEF" for character in text):
        return None
    return ImageInput.from_path(text, max_bytes)


def _message_segments(message: Mapping[str, Any]) -> List[Any]:
    """返回消息段列表，兼容 raw_message 与 message_segments。"""

    segments = message.get("raw_message")
    if not isinstance(segments, list):
        segments = message.get("message_segments")
    return segments if isinstance(segments, list) else []


def _iter_segments(segments: List[Any]) -> Iterator[Mapping[str, Any]]:
    """递归遍历消息段，包含合并转发中的子消息段。"""

    for segment in segments:
        if not isinstance(segment, Mapping):
            continue
        yield segment
        if segment.get("type") != "forward":
            continue
        data = segment.get("data")
        if not isinstance(data, list):
            continue
        for node in data:
            if not isinstance(node, Mapping):
                continue
            nested = node.get("segments") or node.get("content")
            if isinstance(nested, list):
                yield from _iter_segments(nested)

"""群消息中的搜图命令解析。"""

from typing import Optional

import re

from .models import MODE_ANIME, MODE_ILLUSTRATION

_COMMAND_PATTERN = re.compile(
    r"(?:^|\s)(?:[/／#])?(?P<command>搜图片|搜图|搜番|image_search|anime_search)(?=\s|$)",
    re.IGNORECASE,
)
_ANIME_HINT_PATTERN = re.compile(r"(?:番剧|动画|新番|anime)", re.IGNORECASE)
_ILLUSTRATION_HINT_PATTERN = re.compile(r"(?:插画|插畫|二次元|illustration)", re.IGNORECASE)


def parse_search_command(text: object) -> Optional[str]:
    """解析消息中的搜图命令并返回查询模式。

    Args:
        text: 消息纯文本。

    Returns:
        Optional[str]: ``anime`` 或 ``illustration``；没有命令时返回 ``None``。
    """

    normalized = str(text or "")
    match = _COMMAND_PATTERN.search(normalized)
    if match is None:
        return None

    command = match.group("command").lower()
    if command in {"搜番", "anime_search"}:
        return MODE_ANIME

    remainder = normalized[match.end() :]
    if _ANIME_HINT_PATTERN.search(remainder):
        return MODE_ANIME
    if _ILLUSTRATION_HINT_PATTERN.search(remainder):
        return MODE_ILLUSTRATION
    return MODE_ILLUSTRATION

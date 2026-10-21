"""图片输入封装与安全脱敏辅助。"""

from dataclasses import dataclass
from pathlib import Path
from typing import Optional
from urllib.parse import urlsplit

import base64
import binascii
import hashlib

_DATA_URL_PREFIX = "data:image/"
_BASE64_URL_PREFIX = "base64://"


@dataclass(frozen=True)
class ImageInput:
    """待反查的图片，只允许 URL、本地路径、二进制三者之一。"""

    url: Optional[str] = None
    path: Optional[Path] = None
    data: Optional[bytes] = None

    def __post_init__(self) -> None:
        """校验输入互斥且有内容。"""

        provided = [value is not None for value in (self.url, self.path, self.data)]
        if sum(provided) != 1:
            raise ValueError("图片输入必须且只能提供 URL、本地路径、二进制数据中的一种")
        if self.url is not None:
            parsed = urlsplit(self.url)
            if parsed.scheme not in {"http", "https"} or not parsed.netloc:
                raise ValueError("图片 URL 必须为 http:// 或 https:// 地址")
        if self.data is not None and not self.data:
            raise ValueError("图片二进制数据不能为空")

    @classmethod
    def from_url(cls, url: str) -> "ImageInput":
        """从 HTTP(S) URL 构造输入。"""

        return cls(url=str(url).strip())

    @classmethod
    def from_path(cls, path: str | Path, max_bytes: int) -> "ImageInput":
        """从本地路径构造输入，并限制文件大小。"""

        resolved = Path(path).expanduser()
        if not resolved.is_file():
            raise ValueError("图片文件不存在或不是普通文件")
        size = resolved.stat().st_size
        if size <= 0:
            raise ValueError("图片文件为空")
        if size > max_bytes:
            raise ValueError(f"图片文件超过大小限制（最大 {max_bytes} 字节）")
        return cls(path=resolved)

    @classmethod
    def from_bytes(cls, data: bytes, max_bytes: int) -> "ImageInput":
        """从二进制数据构造输入。"""

        if not isinstance(data, (bytes, bytearray)):
            raise ValueError("图片数据必须为 bytes")
        normalized = bytes(data)
        if not normalized:
            raise ValueError("图片数据为空")
        if len(normalized) > max_bytes:
            raise ValueError(f"图片数据超过大小限制（最大 {max_bytes} 字节）")
        return cls(data=normalized)

    @classmethod
    def from_base64(cls, value: str, max_bytes: int) -> "ImageInput":
        """从 Base64 字符串（支持 data URL 前缀）构造输入。"""

        text = "".join(str(value or "").split())
        if not text:
            raise ValueError("图片 Base64 数据为空")
        if text.startswith(_BASE64_URL_PREFIX):
            text = text[len(_BASE64_URL_PREFIX) :]
        elif text.startswith(_DATA_URL_PREFIX) and "," in text:
            text = text.split(",", 1)[1]
        estimated_bytes = len(text) * 3 // 4
        if estimated_bytes > max_bytes + 3:
            raise ValueError(f"图片数据超过大小限制（最大 {max_bytes} 字节）")
        try:
            data = base64.b64decode(text, validate=True)
        except (binascii.Error, ValueError) as exc:
            raise ValueError("图片 Base64 数据无效") from exc
        return cls.from_bytes(data, max_bytes)

    def fingerprint(self) -> str:
        """返回可用于日志关联的短指纹，不暴露原路径或 URL。"""

        digest = hashlib.sha256()
        if self.data is not None:
            digest.update(self.data)
        elif self.path is not None:
            with self.path.open("rb") as image_file:
                for chunk in iter(lambda: image_file.read(1024 * 1024), b""):
                    digest.update(chunk)
        else:
            digest.update(str(self.url).encode("utf-8"))
        return f"sha256:{digest.hexdigest()[:12]}"

    def search_kwargs(self) -> dict[str, object]:
        """转换为 PicImageSearch 的 search() 参数。"""

        if self.url is not None:
            return {"url": self.url}
        if self.path is not None:
            return {"file": self.path}
        return {"file": self.data}

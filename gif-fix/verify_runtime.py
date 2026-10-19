"""验证部署源码、目标 GIF 缓存和历史组件，不发群消息、不调用模型。"""

from contextlib import redirect_stdout, redirect_stderr
import asyncio
import io
import json
import os
from pathlib import Path
import sqlite3
import sys

import msgpack
from PIL import Image

from deploy import contents

ROOT = Path("/home/qqbot/QQbot/MaiBot")
TARGET = "cadf68e77a97802a7093831a2c5988af041fbc221ab1cb82552b65c8501f5f1b"


async def verify():
    """核对实际部署文件和原生读取入口，防止只改缓存却漏掉消息上下文。"""
    for relative, expected in contents(ROOT).items():
        assert (ROOT / relative).read_text(encoding="utf-8") == expected, relative
    connection = sqlite3.connect((ROOT / "data/MaiBot.db").as_uri() + "?mode=ro", uri=True)
    connection.row_factory = sqlite3.Row
    image = connection.execute("SELECT * FROM images WHERE image_hash=?", (TARGET,)).fetchone()
    description = image["description"]
    assert "看看你" in description and "语气需结合上下文" in description
    assert not any(tag in description for tag in ["无语", "嫌弃", "敷衍"])
    expected = f"[表情包: {description}]"
    message_ids = []
    for row in connection.execute("SELECT message_id,raw_content,processed_plain_text FROM mai_messages WHERE raw_content IS NOT NULL"):
        components = msgpack.unpackb(row["raw_content"], raw=False)
        for component in components:
            if component.get("type") == "emoji" and component.get("hash") == TARGET:
                assert component["data"] == expected
                assert expected in row["processed_plain_text"]
                message_ids.append(row["message_id"])
    assert len(message_ids) >= 2
    connection.close()
    os.chdir(ROOT)
    sys.path.insert(0, str(ROOT))
    from src.common.utils.utils_image import ImageUtils
    from src.emoji_system.emoji_manager import emoji_manager
    raw = (ROOT / image["full_path"]).read_bytes()
    with Image.open(io.BytesIO(raw)) as original:
        assert original.n_frames == 45
    with Image.open(io.BytesIO(ImageUtils.gif_2_static_image(raw))) as preview:
        assert preview.size == (1024, 840)
    cached = await emoji_manager.get_emoji_description(emoji_bytes=raw)
    assert cached[0] == description
    return {"source_verified": True, "cached_description": description,
            "historical_messages": message_ids, "original_frames": 45, "preview_frames": 12,
            "cache_entrypoint_verified": True}


if __name__ == "__main__":
    with redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()):
        result = asyncio.run(verify())
    print(json.dumps(result, ensure_ascii=False, indent=2))

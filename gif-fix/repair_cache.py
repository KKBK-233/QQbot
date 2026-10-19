"""按媒体哈希更正识别缓存和历史消息的机器描述，保留原 GIF 与真人文字。"""

import argparse
import base64
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import sqlite3

import msgpack


def replace_descriptions(components, text, image_hash, description):
    """按组件顺序定位描述，避免同一句标签的其他媒体或真人文字被替换。"""
    replacements = []
    cursor = 0
    for component in components:
        old = component.get("data", "")
        position = text.find(old, cursor) if isinstance(old, str) and old else -1
        if position >= 0:
            cursor = position + len(old)
        if component.get("type") != "emoji" or component.get("hash") != image_hash:
            continue
        if not isinstance(old, str) or not old.startswith("[表情包") or position < 0:
            raise ValueError("无法精确定位目标机器描述，拒绝改写")
        new = f"[表情包: {description}]"
        if old != new:
            component["data"] = new
            replacements.append((position, position + len(old), new))
    for start, end, new in reversed(replacements):
        text = text[:start] + new + text[end:]
    return bool(replacements), text


def repair(root, image_hash, description, source_note):
    """备份精确命中的记录，再在一个事务中修改自动生成的描述。"""
    connection = sqlite3.connect(root / "data/MaiBot.db", timeout=10)
    connection.row_factory = sqlite3.Row
    try:
        connection.execute("BEGIN IMMEDIATE")
        image = connection.execute("SELECT * FROM images WHERE image_hash=? AND image_type='EMOJI'", (image_hash,)).fetchone()
        if image is None:
            raise ValueError("未找到指定表情包")
        path = Path(image["full_path"])
        if not path.is_absolute():
            path = root / path
        if hashlib.sha256(path.read_bytes()).hexdigest() != image_hash:
            raise ValueError("原表情文件哈希不匹配")
        changes = []
        # 消息级 is_emoji 标志可能为零；以实际组件类型和媒体哈希为准。
        for row in connection.execute("SELECT id,raw_content,processed_plain_text FROM mai_messages WHERE raw_content IS NOT NULL"):
            components = msgpack.unpackb(row["raw_content"], raw=False)
            text = row["processed_plain_text"] or ""
            changed, text = replace_descriptions(components, text, image_hash, description)
            if changed:
                changes.append((dict(row), msgpack.packb(components, use_bin_type=True), text))

        backup = root / "data/gif-fix-backups" / datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ") / "cache.json"
        backup.parent.mkdir(parents=True, exist_ok=True)
        original_messages = []
        for row, _, _ in changes:
            original_messages.append({**row, "raw_content": base64.b64encode(row["raw_content"]).decode()})
        backup.write_text(json.dumps({"image": dict(image), "messages": original_messages,
                                     "correction": {"description": description, "source": source_note}},
                                    ensure_ascii=False, indent=2), encoding="utf-8")
        connection.execute("UPDATE images SET description=?,vlm_processed=1 WHERE id=?", (description, image["id"]))
        for row, binary, text in changes:
            connection.execute("UPDATE mai_messages SET raw_content=?,processed_plain_text=? WHERE id=?", (binary, text, row["id"]))
        connection.commit()
        print(json.dumps({"description": description, "messages_corrected": len(changes), "backup": str(backup)}, ensure_ascii=False))
    finally:
        connection.close()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=Path("/home/qqbot/QQbot/MaiBot"))
    parser.add_argument("--hash", required=True)
    parser.add_argument("--description", required=True)
    parser.add_argument("--source", required=True)
    args = parser.parse_args()
    repair(args.root, args.hash, args.description, args.source)

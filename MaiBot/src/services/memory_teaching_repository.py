"""原生记忆教学查询适配器，内部表结构只在服务边界维护。"""

from pathlib import Path
import json
import sqlite3
import time


def load_teachings(database: Path, teacher_id: str | None, hashes=None):
    """精确查询有效教师记录；结构或数据错误显式抛出，不伪装为没有教学。"""
    if not teacher_id:
        return []
    if not database.is_file():
        raise RuntimeError("原生教学记忆数据库尚未准备")
    with sqlite3.connect(database.resolve().as_uri() + "?mode=ro", uri=True) as db:
        rows = db.execute(
            "SELECT hash,metadata FROM paragraphs WHERE is_deleted=0 "
            "AND (expires_at IS NULL OR expires_at>?) AND json_valid(metadata) "
            "AND json_extract(metadata,'$.learning_kind')='owner_teaching' "
            "AND json_extract(metadata,'$.teacher_user_id')=? "
            "AND json_extract(metadata,'$.teacher_platform')='qq' ORDER BY created_at DESC LIMIT 200",
            (time.time(), teacher_id),
        ).fetchall()
    result = []
    for hash_value, raw in rows:
        if hashes is not None and hash_value not in hashes:
            continue
        item = json.loads(raw)
        if not isinstance(item.get("teaching_summary"), str) or not isinstance(item.get("teaching_keywords"), list):
            raise ValueError("原生教学记忆缺少总结或关键词")
        result.append({**item, "hash": hash_value})
    return result

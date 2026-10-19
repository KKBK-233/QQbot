"""验证缓存更正只匹配目标媒体，其他标签和真人文字保持。"""

import unittest
from contextlib import redirect_stdout
import hashlib
import io
from pathlib import Path
import sqlite3
import tempfile

import msgpack

from repair_cache import repair, replace_descriptions


class CacheTests(unittest.TestCase):
    def test_identical_labels_from_other_media_are_preserved(self):
        old = "[表情包: 无语]"
        components = [{"type": "text", "data": old},
                      {"type": "emoji", "data": old, "hash": "other"},
                      {"type": "emoji", "data": old, "hash": "target"}]
        changed, text = replace_descriptions(components, old * 3, "target", "看看你")
        self.assertTrue(changed)
        self.assertEqual(text, old * 2 + "[表情包: 看看你]")
        self.assertEqual(components[0]["data"], old)
        self.assertEqual(components[1]["data"], old)

    def test_repeated_target_and_idempotence(self):
        components = [{"type": "emoji", "data": "[表情包]", "hash": "target"}] * 2
        # 独立的组件对象对应两次出现。
        components = [dict(component) for component in components]
        changed, text = replace_descriptions(components, "[表情包] [表情包]", "target", "看看你")
        self.assertTrue(changed)
        self.assertEqual(text, "[表情包: 看看你] [表情包: 看看你]")
        self.assertFalse(replace_descriptions(components, text, "target", "看看你")[0])

    def test_missing_description_fails_closed(self):
        with self.assertRaises(ValueError):
            replace_descriptions([{"type": "emoji", "data": "[表情包]", "hash": "target"}], "真人文字", "target", "看看你")

    def test_media_component_overrides_message_flag(self):
        """实际入站记录的 is_emoji=0，仍应按组件哈希修正并保存恢复证据。"""
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "data").mkdir()
            (root / "data/emoji.gif").write_bytes(b"original")
            target = hashlib.sha256(b"original").hexdigest()
            connection = sqlite3.connect(root / "data/MaiBot.db")
            connection.executescript("CREATE TABLE images (id INTEGER, image_hash TEXT, image_type TEXT, full_path TEXT, description TEXT, vlm_processed INTEGER); CREATE TABLE mai_messages (id INTEGER, is_emoji INTEGER, raw_content BLOB, processed_plain_text TEXT);")
            connection.execute("INSERT INTO images VALUES (1,?,'EMOJI','data/emoji.gif','无语',0)", (target,))
            raw = msgpack.packb([{"type": "emoji", "hash": target, "data": "[表情包: 无语]"}], use_bin_type=True)
            connection.execute("INSERT INTO mai_messages VALUES (1,0,?,'[表情包: 无语]')", (raw,))
            connection.commit()
            with redirect_stdout(io.StringIO()):
                repair(root, target, "看看你", "人工纠正")
            self.assertEqual(connection.execute("SELECT processed_plain_text FROM mai_messages").fetchone()[0], "[表情包: 看看你]")
            self.assertEqual(connection.execute("SELECT description,vlm_processed FROM images").fetchone(), ("看看你", 1))
            self.assertEqual(len(list((root / "data/gif-fix-backups").glob("*/cache.json"))), 1)
            connection.close()


if __name__ == "__main__":
    unittest.main()

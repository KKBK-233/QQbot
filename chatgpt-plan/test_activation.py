"""验证批量模型替换保留候选顺序、禁用任务和回退范围。"""

from pathlib import Path
import json
import tempfile
import unittest

import tomlkit

from deploy import replace_haiku_tasks, restore_haiku


class TaskSwitchTests(unittest.TestCase):
    def config(self):
        return tomlkit.parse('''
[[models]]
name = "custom-fast"
model_identifier = "claude-haiku-5-5"
[[models]]
name = "haiku-low"
model_identifier = "claude-haiku-5-5"
[[models]]
name = "other"
model_identifier = "other"
[model_task_config.planner]
model_list = ["custom-fast", "other", "haiku-low"]
max_tokens = 8192
[model_task_config.memory]
model_list = ["haiku-low"]
[model_task_config.utils]
model_list = ["other"]
[model_task_config.embedding]
model_list = []
''')

    def test_all_active_haiku_tasks_preserve_other_candidates(self):
        doc = self.config()
        previous = replace_haiku_tasks(doc, "luna")
        self.assertEqual(set(previous), {"planner", "memory"})
        self.assertEqual(doc["model_task_config"]["planner"]["model_list"], ["luna", "other"])
        self.assertEqual(doc["model_task_config"]["memory"]["model_list"], ["luna"])
        self.assertEqual(doc["model_task_config"]["planner"]["max_tokens"], 8192)
        self.assertEqual(doc["model_task_config"]["utils"]["model_list"], ["other"])
        self.assertEqual(doc["model_task_config"]["embedding"]["model_list"], [])
        self.assertEqual(previous["planner"], ["custom-fast", "other", "haiku-low"])
        self.assertEqual(replace_haiku_tasks(doc, "luna"), {})

    def test_restore_preserves_unrelated_later_changes(self):
        doc = self.config()
        previous = replace_haiku_tasks(doc, "luna")
        doc["model_task_config"]["utils"]["model_list"] = ["later-model"]
        doc["model_task_config"]["planner"]["max_tokens"] = 1000
        with tempfile.TemporaryDirectory() as directory:
            config = Path(directory) / "config.toml"
            manifest = Path(directory) / "switch.json"
            config.write_text(tomlkit.dumps(doc))
            manifest.write_text(json.dumps({"previous_lists": previous}))
            restore_haiku(config, manifest)
            restored = tomlkit.parse(config.read_text())
        self.assertEqual(restored["model_task_config"]["planner"]["model_list"], previous["planner"])
        self.assertEqual(restored["model_task_config"]["memory"]["model_list"], previous["memory"])
        self.assertEqual(restored["model_task_config"]["utils"]["model_list"], ["later-model"])
        self.assertEqual(restored["model_task_config"]["planner"]["max_tokens"], 1000)


if __name__ == "__main__":
    unittest.main()

"""多仓真实 Git 副本、封装候选、发布事务与恢复回归；不调用模型或生产服务。"""

from dataclasses import replace
from unittest.mock import patch
import json
import subprocess
import sys
import unittest

from dev_worker import project, source_release
from dev_worker.store import atomic_json
from dev_worker.worker import Worker
import test_dev_worker as fixtures


class ProjectReleaseTests(unittest.TestCase):
    git = fixtures.WorkerTests.git

    def setUp(self):
        fixtures.WorkerTests.setUp(self)
        self.extra = self.root / "plugin"
        self.extra.mkdir()
        for args in [
            ("init", "-q"),
            ("config", "user.name", "test"),
            ("config", "user.email", "test@localhost"),
        ]:
            subprocess.check_call(["git", "-C", str(self.extra), *args])
        (self.extra / "feature.py").write_text("VALUE = 1\n")
        (self.extra / ".env").write_text("private credential not tracked")
        subprocess.check_call(["git", "-C", str(self.extra), "add", "feature.py"])
        subprocess.check_call(
            ["git", "-C", str(self.extra), "commit", "-qm", "baseline"]
        )
        (self.source / ".gitignore").write_text("plugins/\n")
        self.git("add", ".gitignore")
        self.git("commit", "-qm", "ignore plugin directories")
        self.live = self.root / "live"
        self.live.mkdir()
        (self.live / "original.txt").write_text("baseline\n")
        (self.live / "plugins/demo").mkdir(parents=True)
        (self.live / "plugins/demo/feature.py").write_text("VALUE = 1\n")
        self.policy = {
            "version": 1,
            "components": [
                {"mount": "plugins/demo", "source": str(self.extra), "ref": "HEAD"}
            ],
            "mappings": [
                {
                    "workspace": ".",
                    "live": str(self.live),
                    "include": ["."],
                    "service": "bot",
                },
                {
                    "workspace": "plugins/demo",
                    "live": str(self.live / "plugins/demo"),
                    "include": ["."],
                    "auto_publish": True,
                    "service": "bot",
                },
            ],
            "non_runtime": [".project-context.json", "docs", "pytests"],
            "services": {
                "bot": {
                    op: [[sys.executable, "-c", "pass"]]
                    for op in ("stop", "start", "health")
                }
            },
        }
        self.policy_file = self.root / "project.json"
        atomic_json(self.policy_file, self.policy)
        self.cfg = replace(
            self.cfg, project_manifest=self.policy_file, system_python=sys.executable
        )

    def candidate(
        self,
        code="from pathlib import Path; Path('plugins/demo/feature.py').write_text('VALUE = 2\\n'); Path('plugins/demo/new.py').write_text('NEW = True\\n')",
        *,
        tests_pass=True,
    ):
        worker = Worker(self.cfg)
        task = worker.submit("1020050112", "实现一个插件改动")
        folder = worker.store.directory(task["id"])

        def prepared(*_):
            (folder / "venv").mkdir()
            (folder / "agent-home").mkdir()

        agent = (
            code
            + "; import json; print(json.dumps({'type':'session','sessionId':'session-test'})); print(json.dumps({'type':'final','text':'done'}))"
        )
        with (
            patch("dev_worker.worker.prepare_environment", side_effect=prepared),
            patch(
                "dev_worker.worker.agent_command",
                return_value=[sys.executable, "-c", agent],
            ),
            patch(
                "dev_worker.worker.test_command",
                return_value=[sys.executable, "-c", "assert " + str(tests_pass)],
            ),
        ):
            result = worker.run_once()
        return worker, result, folder

    def test_composed_baseline_is_pinned_and_new_ignored_plugin_file_is_collected(self):
        worker, task, folder = self.candidate()
        self.assertEqual(task["status"], "succeeded", task)
        self.assertEqual(
            task["result"]["patch"]["files"],
            ["plugins/demo/feature.py", "plugins/demo/new.py"],
        )
        self.assertFalse((folder / "ws/plugins/demo/.git").exists())
        self.assertFalse((folder / "ws/plugins/demo/.env").exists())
        self.assertEqual((self.extra / "feature.py").read_text(), "VALUE = 1\n")
        self.assertEqual(
            (self.live / "plugins/demo/feature.py").read_text(), "VALUE = 1\n"
        )
        self.assertTrue(task["result"]["source_candidate"])

    def test_publish_uses_sealed_bytes_and_rollback_preserves_developer_workspace(self):
        worker, task, folder = self.candidate()
        (folder / "ws/plugins/demo/feature.py").write_text("VALUE = 999\n")
        release = source_release.publish(self.cfg, worker.store, task["id"])
        self.assertEqual(release["state"], "installed", release)
        self.assertEqual(
            (self.live / "plugins/demo/feature.py").read_text(), "VALUE = 2\n"
        )
        self.assertTrue((self.live / "plugins/demo/new.py").exists())
        self.assertEqual(
            worker.store.source_publication(task["id"])["state"], "installed"
        )
        source_release.rollback(self.cfg, release["id"])
        self.assertEqual(
            (self.live / "plugins/demo/feature.py").read_text(), "VALUE = 1\n"
        )
        self.assertFalse((self.live / "plugins/demo/new.py").exists())
        self.assertEqual(
            (folder / "ws/plugins/demo/feature.py").read_text(), "VALUE = 999\n"
        )

    def test_test_failure_never_creates_publishable_candidate(self):
        worker, task, folder = self.candidate(tests_pass=False)
        self.assertEqual(task["status"], "failed")
        self.assertFalse((folder / "source-candidate").exists())
        with self.assertRaises(source_release.SourceReleaseError):
            source_release.publish(self.cfg, worker.store, task["id"])

    def test_missing_publication_mapping_does_not_rewrite_development_result(self):
        self.policy["mappings"][0]["include"] = ["original.txt"]
        atomic_json(self.policy_file, self.policy)
        worker, task, _ = self.candidate(
            "from pathlib import Path; Path('unmapped.py').write_text('VALUE = 1\\n')"
        )
        self.assertEqual(task["status"], "succeeded", task)
        self.assertEqual(task["result"]["test_exit"], 0)
        self.assertEqual(task["result"]["source_candidate"]["state"], "blocked")
        with self.assertRaises(source_release.SourceReleaseError):
            source_release.publish(self.cfg, worker.store, task["id"], allow_core=True)

    def test_live_or_sealed_candidate_changes_block_release(self):
        worker, task, folder = self.candidate()
        (self.live / "plugins/demo/feature.py").write_text("manual edit\n")
        with self.assertRaisesRegex(source_release.SourceReleaseError, "线上基线"):
            source_release.publish(self.cfg, worker.store, task["id"])
        (self.live / "plugins/demo/feature.py").write_text("VALUE = 1\n")
        (folder / "source-candidate/files/plugins/demo/new.py").write_text("tampered")
        with self.assertRaisesRegex(source_release.SourceReleaseError, "候选字节"):
            source_release.publish(self.cfg, worker.store, task["id"])

    def test_failed_new_version_health_restores_old_version(self):
        self.policy["services"]["bot"]["health"] = [
            [
                sys.executable,
                "-c",
                f"from pathlib import Path; assert Path({str(self.live / 'plugins/demo/feature.py')!r}).read_text() == 'VALUE = 1\\n'",
            ]
        ]
        atomic_json(self.policy_file, self.policy)
        worker, task, _ = self.candidate()
        release = source_release.publish(self.cfg, worker.store, task["id"])
        self.assertEqual(release["state"], "rolled_back", release)
        self.assertEqual(
            (self.live / "plugins/demo/feature.py").read_text(), "VALUE = 1\n"
        )

    def test_core_requires_explicit_maintenance_publish(self):
        worker, task, _ = self.candidate(
            "from pathlib import Path; Path('original.txt').write_text('new')"
        )
        with self.assertRaisesRegex(source_release.SourceReleaseError, "核心候选"):
            source_release.publish(self.cfg, worker.store, task["id"])
        self.assertEqual(
            source_release.publish(self.cfg, worker.store, task["id"], allow_core=True)[
                "state"
            ],
            "installed",
        )

    def test_auto_release_is_limited_to_configured_plugin_and_reported_separately(self):
        self.policy["auto_source_release"] = True
        atomic_json(self.policy_file, self.policy)
        worker, task, _ = self.candidate()
        self.assertEqual(task["reason"], "verified_patch")
        self.assertEqual(
            worker.store.source_publication(task["id"])["state"], "installed"
        )

    def test_rollback_refuses_post_release_manual_changes(self):
        worker, task, _ = self.candidate()
        released = source_release.publish(self.cfg, worker.store, task["id"])
        (self.live / "plugins/demo/feature.py").write_text("manual edit")
        with self.assertRaisesRegex(source_release.SourceReleaseError, "其他修改"):
            source_release.rollback(self.cfg, released["id"])

    def test_recover_interrupted_install_restores_partially_written_files(self):
        worker, task, _ = self.candidate()
        released = source_release.publish(self.cfg, worker.store, task["id"])
        folder, manifest = source_release._read_release(self.cfg, released["id"])
        manifest["state"] = "installing"
        atomic_json(folder / "manifest.json", manifest)
        (self.live / "plugins/demo/feature.py").write_text("VALUE = 1\n")
        restored = source_release.rollback(self.cfg, released["id"], recover=True)
        self.assertEqual(restored["state"], "rolled_back")
        self.assertFalse((self.live / "plugins/demo/new.py").exists())

    def test_dependency_candidate_installs_into_version_and_restores_pointer(self):
        self.cfg = replace(self.cfg, test_timeout_sec=60)
        (self.source / "requirements.txt").write_text("# empty local fixture\n")
        self.git("add", "requirements.txt")
        self.git("commit", "-qm", "dependency fixture")
        site = self.live / "site-packages"
        site.mkdir()
        (self.live / "requirements.txt").write_text("# empty local fixture\n")
        self.policy["dependencies"] = {
            "when": ["requirements.txt"],
            "requirements": "requirements.txt",
            "site": str(site),
            "service": "bot",
            "checks": [["-c", "import json"]],
        }
        atomic_json(self.policy_file, self.policy)
        worker, task, folder = self.candidate(
            "from pathlib import Path; Path('requirements.txt').write_text('# updated fixture\\n')"
        )
        self.assertEqual(task["status"], "succeeded", task)
        self.assertTrue((folder / "source-candidate/inventory.json").exists())
        released = source_release.publish(
            self.cfg, worker.store, task["id"], allow_core=True
        )
        self.assertEqual(released["state"], "installed", released)
        pointer = site / "00-kkbk-source-release.pth"
        self.assertIn("runtime-venv", pointer.read_text())
        source_release.rollback(self.cfg, released["id"])
        self.assertFalse(pointer.exists())

    def test_worker_restart_recovers_once_and_reconciles_release_receipt(self):
        worker, task, _ = self.candidate()
        released = source_release.publish(self.cfg, worker.store, task["id"])
        folder, manifest = source_release._read_release(self.cfg, released["id"])
        manifest["state"] = "installing"
        atomic_json(folder / "manifest.json", manifest)
        self.assertIsNone(worker.run_once())
        self.assertEqual(
            worker.store.source_publication(task["id"])["state"], "rolled_back"
        )
        self.assertEqual(
            (self.live / "plugins/demo/feature.py").read_text(), "VALUE = 1\n"
        )
        self.assertIsNone(worker.run_once())

    def test_live_code_overlay_is_baseline_not_candidate_change(self):
        self.policy["overlay_live_code"] = True
        atomic_json(self.policy_file, self.policy)
        (self.live / "plugins/demo/feature.py").write_text("VALUE = 10\n")
        worker, task, folder = self.candidate(
            "from pathlib import Path; assert '10' in Path('plugins/demo/feature.py').read_text(); Path('plugins/demo/new.py').write_text('NEW = 1\\n')"
        )
        self.assertEqual(task["status"], "succeeded", task)
        self.assertEqual(task["result"]["patch"]["files"], ["plugins/demo/new.py"])

    def test_stale_live_tests_do_not_replace_source_or_enter_release(self):
        tests = self.extra / "tests"
        tests.mkdir()
        (tests / "test_feature.py").write_text("CURRENT = True\n")
        subprocess.check_call(["git", "-C", str(self.extra), "add", "tests"])
        subprocess.check_call(
            ["git", "-C", str(self.extra), "commit", "-qm", "current tests"]
        )
        live_tests = self.live / "plugins/demo/tests"
        live_tests.mkdir()
        (live_tests / "test_feature.py").write_text("STALE = True\n")
        self.policy["overlay_live_code"] = True
        atomic_json(self.policy_file, self.policy)
        code = "from pathlib import Path; p=Path('plugins/demo/tests/test_feature.py'); assert 'CURRENT' in p.read_text(); p.write_text('UPDATED = True\\n'); Path('plugins/demo/new.py').write_text('NEW = 1\\n')"
        worker, task, folder = self.candidate(code)
        self.assertEqual(task["status"], "succeeded", task)
        manifest = json.loads((folder / "source-candidate/candidate.json").read_text())
        self.assertEqual(
            [r["name"] for r in manifest["files"]], ["plugins/demo/new.py"]
        )
        self.assertIn("plugins/demo/tests/test_feature.py", manifest["non_runtime"])

    def test_path_and_config_snapshot_boundaries(self):
        for name in (
            ".",
            "../bad",
            "/absolute",
            "C:/bad",
            "ok/../../bad",
            ".git/config",
            "a\\b",
        ):
            with self.assertRaises(ValueError):
                project.relative(name)
        config = self.root / "bot.toml"
        config.write_text(
            '[personality]\nname="test"\n[models]\napi_key="do not copy"\n'
        )
        safe = {
            "snapshots": [
                {"name": "persona", "source": str(config), "fields": ["personality"]}
            ]
        }
        result = project.configuration_snapshot(safe)
        self.assertNotIn("api_key", json.dumps(result))
        safe["snapshots"][0]["fields"] = ["models"]
        with self.assertRaises(ValueError):
            project.configuration_snapshot(safe)


if __name__ == "__main__":
    unittest.main()

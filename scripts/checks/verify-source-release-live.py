"""显式选择后验证真实源码发布与回退；无模型调用、无群通知，仅增加注释。"""

from pathlib import Path
from unittest.mock import patch
import argparse
import hashlib
import json
import subprocess
import sys

sys.path.insert(0, "/home/qqbot/dev-harness/worker-code")

from dev_worker import source_release
from dev_worker.config import WorkerConfig
from dev_worker.store import exclusive_lock
from dev_worker.worker import Worker
from dev_worker.workspace import sandbox_command


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--apply-and-rollback", action="store_true")
    args = parser.parse_args()
    if not args.apply_and_rollback:
        print(
            json.dumps(
                {
                    "writes": False,
                    "detail": "需要显式 --apply-and-rollback；会停启 MaiBot 并恢复同一批文件",
                }
            )
        )
        return 0
    config = WorkerConfig.from_env()
    worker = Worker(config)
    control = ["supervisorctl", "-c", "/home/qqbot/QQbot/supervisord.conf"]
    live = Path("/home/qqbot/QQbot/MaiBot/plugins/image_reverse_search/plugin.py")
    original = hashlib.sha256(live.read_bytes()).hexdigest()
    with exclusive_lock(config.lock_path):
        with worker.store.connect() as db:
            if db.execute(
                "select count(*) from tasks where status in ('queued','running')"
            ).fetchone()[0]:
                raise RuntimeError("已有真实开发任务，不执行发布演练")
        subprocess.run(
            [*control, "stop", "dev-worker"],
            check=True,
            capture_output=True,
            timeout=30,
        )
        released = None
        try:
            task = worker.submit(
                "1020050112",
                "维护端发布闭环演练：增加注释、验证、发布后立即回退；无模型，无群通知",
            )
            folder = worker.store.directory(task["id"])
            code = (
                "from pathlib import Path; import json; p=Path('plugins/image_reverse_search/plugin.py'); "
                "p.write_bytes(p.read_bytes()+b'\\n# source release lifecycle smoke\\n'); "
                "print(json.dumps({'type':'session','sessionId':'session-source-release-smoke'})); "
                "print(json.dumps({'type':'final','text':'deterministic fixture; no model request'}))"
            )
            command = sandbox_command(
                config, folder, [folder / "venv/bin/python", "-B", "-c", code]
            )
            with patch("dev_worker.worker.agent_command", return_value=command):
                result = worker.execute(worker.store.claim())
            if result["status"] != "succeeded":
                raise RuntimeError(
                    "发布演练候选未验证："
                    + str(result["result"].get("error", result["reason"]))
                )
            assert result["result"]["patch"]["files"] == [
                "plugins/image_reverse_search/plugin.py"
            ]
            released = source_release.publish(config, worker.store, task["id"])
            if released["state"] != "installed":
                raise RuntimeError("发布未通过健康检查：" + json.dumps(released))
            assert hashlib.sha256(live.read_bytes()).hexdigest() != original
            reverted = source_release.rollback(config, released["id"])
            assert reverted["state"] == "rolled_back"
            assert hashlib.sha256(live.read_bytes()).hexdigest() == original
            print(
                json.dumps(
                    {
                        "task_id": task["id"],
                        "release_id": released["id"],
                        "published": "installed",
                        "final_state": "rolled_back",
                        "original_sha256": original,
                        "test_exit": result["result"]["test_exit"],
                        "checks": result["result"]["project_checks"],
                        "model_calls": 0,
                        "qq_messages": 0,
                        "qq_login_restarted": False,
                    },
                    ensure_ascii=False,
                )
            )
        finally:
            if (
                released
                and released["state"] == "installed"
                and hashlib.sha256(live.read_bytes()).hexdigest() != original
            ):
                source_release.rollback(config, released["id"])
            subprocess.run(
                [*control, "start", "dev-worker"],
                check=True,
                capture_output=True,
                timeout=30,
            )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

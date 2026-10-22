"""不调用模型、不部署或发消息，验证真实多仓、bwrap、独立测试及封装产物。"""

from dataclasses import replace
from pathlib import Path
from unittest.mock import patch
import json
import sys
import tempfile

ROOT = Path("/mnt/d/code/web/QQbot")
sys.path.insert(0, str(ROOT))

from dev_worker.config import WorkerConfig
from dev_worker.store import atomic_json
from dev_worker.worker import Worker
from dev_worker.workspace import sandbox_command


def main():
    """单独账本与任务目录，沿用只读依赖基座，自动发布显式关闭。"""
    original = WorkerConfig.from_env()
    folder = Path(tempfile.mkdtemp(prefix="project-smoke-", dir=str(original.root)))
    profile = json.loads((ROOT / "dev_worker/project.wsl.json").read_text())
    profile["auto_source_release"] = False
    atomic_json(folder / "project.json", profile)
    (folder / "venv-base").symlink_to(original.base_venv, target_is_directory=True)
    source = folder / "home/profiles/headless"
    source.mkdir(parents=True)
    (source / "package.json").write_text("{}")
    config = replace(original, root=folder, project_manifest=folder / "project.json")
    worker = Worker(config)
    task = worker.submit("1020050112", "多仓和独立复测集成验证，不调用模型，不发布")
    task_folder = worker.store.directory(task["id"])
    program = (
        "from pathlib import Path; import json; "
        "p=Path('plugins/image_reverse_search/plugin.py'); p.write_bytes(p.read_bytes()+b'\\n# multi-repo smoke\\n'); "
        "print(json.dumps({'type':'session','sessionId':'session-project-smoke'})); "
        "print(json.dumps({'type':'final','text':'deterministic fixture; no model request'}))"
    )
    command = sandbox_command(
        config, task_folder, [task_folder / "venv/bin/python", "-B", "-c", program]
    )
    with patch("dev_worker.worker.agent_command", return_value=command):
        result = worker.run_once()
    if result["status"] != "succeeded":
        print(
            json.dumps(
                {
                    "task_id": task["id"],
                    "status": result["status"],
                    "result": result["result"],
                    "artifacts": str(task_folder),
                },
                ensure_ascii=False,
            )
        )
        return 1
    state = json.loads((task_folder / "project.json").read_text())
    assert result["result"]["patch"]["files"] == [
        "plugins/image_reverse_search/plugin.py"
    ]
    assert worker.store.source_publication(task["id"]) is None
    print(
        json.dumps(
            {
                "task_id": task["id"],
                "status": result["status"],
                "artifacts": str(task_folder),
                "components": [
                    {k: c[k] for k in ("mount", "commit")}
                    for c in task["spec"]["project"]["components"]
                ],
                "overlaid_live_code": len(state["overlaid_live_code"]),
                "test_exit": result["result"]["test_exit"],
                "checks": result["result"]["project_checks"],
                "source_candidate": result["result"]["source_candidate"],
                "published": False,
                "model_calls": 0,
            },
            ensure_ascii=False,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

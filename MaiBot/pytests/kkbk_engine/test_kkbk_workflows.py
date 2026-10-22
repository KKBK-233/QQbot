"""版本化工作流的启用边界、配置诊断和任务快照回归。"""

from dataclasses import FrozenInstanceError
from pathlib import Path

import hashlib
import json

import pytest

from src.kkbk_engine.workflows import (
    DEFAULT_WORKFLOW_ROOT,
    MAX_FILE_BYTES,
    WorkflowConfigError,
    build_workflow_guidance,
    load_workflow_snapshot,
)


def write_json(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, ensure_ascii=False), encoding="utf-8")


def workflow(version="1.0.0", step="先确认实际来源"):
    return {"schema_version": 1, "id": "research", "version": version,
            "purpose": "查证事实", "scope": "仅限使用联网工具的查证任务",
            "tools": ["web_search", "read_webpage"], "steps": [step]}


def entry(version="1.0.0"):
    return {"id": "research", "version": version, "file": f"catalog/research/{version}.json"}


def setup_files(root, *, version="1.0.0", step="先确认实际来源"):
    write_json(root / entry(version)["file"], workflow(version, step))
    write_json(root / "enabled.json", {"schema_version": 1, "enabled": [entry(version)]})


def test_version_switch_only_changes_later_snapshots(tmp_path):
    setup_files(tmp_path)
    first = load_workflow_snapshot(tmp_path)
    first_guidance = build_workflow_guidance(first, frozenset({"web_search", "read_webpage"}))
    setup_files(tmp_path, version="1.1.0", step="新增来源比较")
    second = load_workflow_snapshot(tmp_path)
    assert first.workflows[0].version == "1.0.0"
    assert second.workflows[0].version == "1.1.0"
    assert first.workflows[0].sha256 != second.workflows[0].sha256
    assert first.manifest_sha256 != second.manifest_sha256
    assert build_workflow_guidance(first, frozenset({"web_search", "read_webpage"})) == first_guidance
    with pytest.raises(FrozenInstanceError):
        first.workflows[0].version = "9.0.0"


def test_same_version_changed_content_is_visible_in_hash(tmp_path):
    setup_files(tmp_path)
    first = load_workflow_snapshot(tmp_path)
    setup_files(tmp_path, step="同版本被改过")
    second = load_workflow_snapshot(tmp_path)
    assert first.workflows[0].version == second.workflows[0].version
    assert first.workflows[0].sha256 != second.workflows[0].sha256
    assert first.manifest_sha256 == second.manifest_sha256
    assert first.record()["workflows"] != second.record()["workflows"]


def test_only_explicitly_enabled_files_are_loaded(tmp_path):
    setup_files(tmp_path)
    (tmp_path / "catalog" / "not-enabled.json").write_text("broken", encoding="utf-8")
    snapshot = load_workflow_snapshot(tmp_path)
    assert not snapshot.error and len(snapshot.workflows) == 1
    write_json(tmp_path / "enabled.json", {"schema_version": 1, "enabled": []})
    assert load_workflow_snapshot(tmp_path).workflows == ()


def test_invalid_config_does_not_reuse_last_good_snapshot(tmp_path):
    setup_files(tmp_path)
    first = load_workflow_snapshot(tmp_path)
    assert first.workflows
    (tmp_path / "enabled.json").write_text("{bad", encoding="utf-8")
    invalid = load_workflow_snapshot(tmp_path)
    assert invalid.error and invalid.workflows == ()
    assert first.workflows
    with pytest.raises(WorkflowConfigError):
        build_workflow_guidance(invalid, frozenset({"web_search", "read_webpage"}))
    setup_files(tmp_path, version="2.0.0")
    assert load_workflow_snapshot(tmp_path).workflows[0].version == "2.0.0"


@pytest.mark.parametrize("file", ["../outside.json", "catalog/../../outside.json", "C:/outside.json",
                                  "/outside.json", "catalog\\..\\outside.json", "enabled.json"])
def test_manifest_path_traversal_is_rejected(tmp_path, file):
    setup_files(tmp_path)
    item = {**entry(), "file": file}
    write_json(tmp_path / "enabled.json", {"schema_version": 1, "enabled": [item]})
    snapshot = load_workflow_snapshot(tmp_path)
    assert snapshot.error and snapshot.workflows == ()


def test_symlink_outside_root_is_rejected(tmp_path):
    root = tmp_path / "root"
    setup_files(root)
    outside = tmp_path / "outside.json"
    write_json(outside, workflow())
    target = root / entry()["file"]
    target.unlink()
    try:
        target.symlink_to(outside)
    except OSError:
        pytest.skip("当前 Windows 账号未获创建符号链接权限")
    assert "越界" in load_workflow_snapshot(root).error


@pytest.mark.parametrize("failure_path", ["root", "manifest", "workflow"])
def test_python312_symlink_loop_error_is_reported(tmp_path, monkeypatch, failure_path):
    setup_files(tmp_path)
    target = {"root": tmp_path, "manifest": tmp_path / "enabled.json",
              "workflow": tmp_path / entry()["file"]}[failure_path]
    original_resolve = Path.resolve

    def resolve(path, *args, **kwargs):
        # 不依赖 Windows 的建链权限，覆盖 Python 3.12 的实际异常类型。
        if path == target:
            raise RuntimeError("Symlink loop")
        return original_resolve(path, *args, **kwargs)

    monkeypatch.setattr(Path, "resolve", resolve)
    snapshot = load_workflow_snapshot(tmp_path)
    assert "RuntimeError: Symlink loop" in snapshot.error
    assert snapshot.workflows == ()


def test_real_symlink_loop_is_reported(tmp_path):
    setup_files(tmp_path)
    target = tmp_path / entry()["file"]
    target.unlink()
    try:
        target.symlink_to(target)
    except OSError:
        pytest.skip("当前 Windows 账号未获创建符号链接权限")
    snapshot = load_workflow_snapshot(tmp_path)
    assert snapshot.error and snapshot.workflows == ()


@pytest.mark.parametrize("change", [
    {"version": "1.0"}, {"id": "wrong"}, {"version": "2.0.0"}, {"schema_version": True},
    {"tools": ["web_search", "web_search"]}, {"tools": []}, {"tools": ["../tool"]},
    {"steps": []}, {"steps": [""]}, {"extra": "unsupported"}, {"purpose": 123},
])
def test_invalid_workflow_fields_fail_atomically(tmp_path, change):
    setup_files(tmp_path)
    write_json(tmp_path / entry()["file"], {**workflow(), **change})
    snapshot = load_workflow_snapshot(tmp_path)
    assert snapshot.error and snapshot.workflows == ()


@pytest.mark.parametrize("enabled", [[entry(), entry()], [entry(), entry("1.1.0")], "research", ["research"]])
def test_invalid_activation_is_rejected(tmp_path, enabled):
    setup_files(tmp_path)
    write_json(tmp_path / "enabled.json", {"schema_version": 1, "enabled": enabled})
    assert load_workflow_snapshot(tmp_path).error


def test_duplicate_json_keys_and_missing_files_are_diagnosed(tmp_path):
    tmp_path.joinpath("enabled.json").write_text(
        '{"schema_version":1,"enabled":[],"enabled":[]}', encoding="utf-8")
    assert "重复" in load_workflow_snapshot(tmp_path).error
    setup_files(tmp_path)
    tmp_path.joinpath(entry()["file"]).unlink()
    assert "FileNotFoundError" in load_workflow_snapshot(tmp_path).error


def test_size_limit_and_missing_manifest_are_diagnosed(tmp_path):
    assert not load_workflow_snapshot(tmp_path / "legacy-no-directory").error
    assert "FileNotFoundError" in load_workflow_snapshot(tmp_path).error
    tmp_path.joinpath("enabled.json").write_bytes(b" " * (MAX_FILE_BYTES + 1))
    assert "超过" in load_workflow_snapshot(tmp_path).error


def test_guidance_uses_explicit_tools_without_task_keyword_matching(tmp_path):
    setup_files(tmp_path)
    snapshot = load_workflow_snapshot(tmp_path)
    assert build_workflow_guidance(snapshot, frozenset({"remember_memory", "run_python"})) == ""
    assert build_workflow_guidance(snapshot, frozenset({"web_search"})) == ""
    guidance = build_workflow_guidance(snapshot, frozenset({"web_search", "read_webpage"}))
    assert "仅限使用联网工具" in guidance
    assert "并非强制 DAG" in guidance and "不改变用户目标" in guidance
    assert snapshot.workflows[0].sha256 in guidance


def test_snapshot_diagnostics_are_copies(tmp_path):
    setup_files(tmp_path)
    snapshot = load_workflow_snapshot(tmp_path)
    record = snapshot.record()
    record["workflows"][0]["version"] = "forged"
    assert snapshot.workflows[0].version == "1.0.0"


def test_shipped_workflow_is_valid_and_only_uses_existing_web_tools():
    snapshot = load_workflow_snapshot(DEFAULT_WORKFLOW_ROOT)
    # 开发副本同步线上启用集合后可有额外工作流；只约束随源码提供的搜索示例。
    assert not snapshot.error
    shipped = next(workflow for workflow in snapshot.workflows if workflow.workflow_id == "conservative-web-research")
    assert shipped.tools == ("web_search", "read_webpage")
    raw = Path(DEFAULT_WORKFLOW_ROOT / "enabled.json").read_bytes()
    assert snapshot.manifest_sha256 == hashlib.sha256(raw).hexdigest()

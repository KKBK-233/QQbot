"""固定多仓源码、线上文件基线和脱敏配置，组合成单一可比较的开发工作树。"""

from dataclasses import replace
from pathlib import Path, PurePosixPath
import hashlib
import json
import re
import subprocess
import tomllib

from .process import run_process
from .store import atomic_json
from .workspace import agent_env, host_env, resolve_commit, sandbox_command


def relative(name, *, root=False):
    """统一清单路径，拒绝反斜杠、父目录及平台相关绝对路径。"""
    if root and name == ".":
        return name
    if (
        not isinstance(name, str)
        or not name
        or name == "."
        or "\\" in name
        or ":" in name
    ):
        raise ValueError("项目路径无效")
    path = PurePosixPath(name)
    if (
        path.is_absolute()
        or any(p in {".", "..", ".git"} for p in path.parts)
        or path.as_posix() != name
    ):
        raise ValueError("项目路径越界：" + name)
    return name


def digest(path):
    """不存在和普通文件分别返回 None/摘要，符号链接不能作为部署目标。"""
    if path.is_symlink():
        raise ValueError("拒绝符号链接：" + str(path))
    if not path.exists():
        return None
    if not path.is_file():
        raise ValueError("不是普通文件：" + str(path))
    return hashlib.sha256(path.read_bytes()).hexdigest()


def target(root, name):
    path = root / relative(name)
    if not path.resolve().is_relative_to(root.resolve()):
        raise ValueError("目标路径越界")
    current = path
    while current != root:
        if current.is_symlink():
            raise ValueError("目标含符号链接")
        current = current.parent
    return path


def covers(name, prefix):
    return prefix == "." or name == prefix or name.startswith(prefix + "/")


def is_test_file(name):
    """测试以提交版本为准，不从生产残留测试覆盖，也不作为运行文件部署。"""
    path = PurePosixPath(name)
    return bool(
        {"tests", "pytests", "__pycache__"} & set(path.parts)
    ) or path.name.startswith("test_")


def git_files(config, repo, commit):
    """读取已提交 blob 清单；不执行 checkout、过滤器或复制未提交文件。"""
    cfg = replace(config, source_repo=Path(repo))
    raw = subprocess.check_output(
        ["git", "-C", repo, "ls-tree", "-rz", commit], env=host_env(cfg), timeout=30
    )
    files = []
    for entry in raw.split(b"\0"):
        if not entry:
            continue
        info, name = entry.split(b"\t", 1)
        mode, kind, oid = info.split()
        name = relative(name.decode("utf-8"))
        if mode not in (b"100644", b"100755") or kind != b"blob":
            raise ValueError("项目清单只接受普通已提交文件：" + name)
        files.append((name, oid.decode(), int(mode, 8) & 0o777))
    return files


def pin(config):
    """提交任务时固定可信项目清单与所有组件提交，代理不能改动宿主发布策略。"""
    if config.project_manifest is None:
        return None
    spec = json.loads(config.project_manifest.read_text(encoding="utf-8"))
    if spec.get("version") != 1:
        raise ValueError("不支持的项目清单版本")
    mounts = []
    for item in spec["components"]:
        mount = relative(item["mount"], root=True)
        if mount == "." or any(
            covers(mount, other) or covers(other, mount) for other in mounts
        ):
            raise ValueError("附加组件不能覆盖主仓或互相嵌套")
        mounts.append(mount)
        repo = Path(item["source"]).resolve()
        item["source"] = str(repo)
        item["commit"] = resolve_commit(
            replace(config, source_repo=repo), item.get("ref", "HEAD")
        )
        # 提前拒绝子模块与符号链接，避免任务运行到一半才发现无法组合。
        git_files(config, str(repo), item["commit"])
    mappings = spec.get("mappings", [])
    prefixes = []
    for item in mappings:
        prefix = relative(item["workspace"], root=True)
        if prefix in prefixes:
            raise ValueError("发布映射重复")
        prefixes.append(prefix)
        live = Path(item["live"]).resolve()
        if not live.is_absolute() or live == Path(live.anchor):
            raise ValueError("发布目录必须是明确的项目目录")
        if live.is_relative_to(config.root.resolve()) and item.get("auto_publish"):
            raise ValueError("开发服务自身的文件不能自动发布")
        item["live"] = str(live)
        for name in item["include"]:
            relative(name, root=True)
    for check in spec.get("checks", []):
        relative(check.get("when", "."), root=True)
        if not check.get("argv") or not all(
            isinstance(arg, str) for arg in check["argv"]
        ):
            raise ValueError("独立测试命令必须是参数数组")
    for patch in spec.get("patches", []):
        relative(patch["file"])
        relative(patch["directory"])
    return spec


def mapping(spec, name):
    """最长前缀决定归属，插件发布不能意外落入宿主代码目录。"""
    matches = [m for m in spec["mappings"] if covers(name, m["workspace"])]
    if not matches:
        raise ValueError("缺少发布映射：" + name)
    item = max(
        matches, key=lambda m: len(m["workspace"]) if m["workspace"] != "." else 0
    )
    rel = name if item["workspace"] == "." else name[len(item["workspace"]) + 1 :]
    relative(rel)
    if not any(covers(rel, prefix) for prefix in item["include"]):
        raise ValueError("发布范围未授权：" + name)
    return item, rel


def live_baseline(spec):
    """保存线上摘要而非正文，供提交到发布期间的并发变更检测。"""
    baseline = {}
    for item in spec["mappings"]:
        root = Path(item["live"])
        for prefix in item["include"]:
            start = root if prefix == "." else target(root, prefix)
            paths = sorted(start.rglob("*")) if start.is_dir() else [start]
            for path in paths:
                if is_test_file(path.relative_to(root).as_posix()):
                    continue
                # 生产目录可能留有 root 所属的旧备份；它们不是运行源码或候选基线。
                if path.suffix.startswith(".before-") or path.suffix in {
                    ".bak",
                    ".old",
                    ".orig",
                    ".pyc",
                    ".db",
                    ".sqlite3",
                }:
                    continue
                if path.is_symlink():
                    raise ValueError("发布范围包含符号链接")
                if (
                    not path.is_file()
                    or "__pycache__" in path.parts
                    or ".git" in path.parts
                ):
                    continue
                rel = path.relative_to(root).as_posix()
                name = (
                    rel if item["workspace"] == "." else item["workspace"] + "/" + rel
                )
                baseline[name] = digest(path)
    return baseline


def configuration_snapshot(spec):
    """只读取管理员显式列出的顶层配置字段；不复制完整配置、令牌或运行数据库。"""
    result = {}
    for item in spec.get("snapshots", []):
        raw = Path(item["source"]).read_bytes()
        data = tomllib.loads(raw.decode("utf-8"))
        # 人设和表达规则之外的配置需要扩展显式 schema，避免误导出模型密钥。
        if not set(item["fields"]) <= {"personality", "expression"}:
            raise ValueError("配置快照仅允许人设和表达字段")
        selected = {key: data[key] for key in item["fields"] if key in data}
        encoded = json.dumps(selected, ensure_ascii=False)
        if re.search(
            r"(?i)api[_-]?key|authorization|password|secret|sk-[A-Za-z0-9]{20}", encoded
        ):
            raise ValueError("配置快照疑似包含凭据，未导出")
        result[item["name"]] = selected
    return result


def compose(config, folder, spec, cancel):
    """主仓仍位于 ws 根；附加仓展开为普通文件后统一提交，后续补丁覆盖全部组件。"""
    workspace = folder / "ws"
    for item in spec["components"]:
        root = target(workspace, item["mount"])
        if root.exists():
            raise ValueError("组件挂载位置已存在：" + item["mount"])
        root.mkdir(parents=True)
        cfg = replace(config, source_repo=Path(item["source"]))
        for name, oid, mode in git_files(config, item["source"], item["commit"]):
            if cancel():
                from .process import Stopped

                raise Stopped("cancelled")
            data = subprocess.check_output(
                ["git", "-C", item["source"], "cat-file", "blob", oid],
                env=host_env(cfg),
                timeout=30,
            )
            path = target(root, name)
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(data)
            path.chmod(mode)
    git = ["git", "-c", "core.hooksPath=/dev/null", "-c", "core.fsmonitor=false"]
    for index, item in enumerate(spec.get("patches", [])):
        # 已提交的维护补丁由可信清单指定，不把源仓的未提交改动整体带入副本。
        command = git + [
            "apply",
            "--directory=" + item["directory"],
            "--",
            str(target(workspace, item["file"])),
        ]
        done = run_process(
            sandbox_command(config, folder, command),
            cwd=workspace,
            env=agent_env(config, folder),
            stdout=folder / f"project-patch-{index}.log",
            stderr=folder / f"project-patch-{index}.err",
            timeout=config.prepare_timeout_sec,
            cancel=cancel,
            max_bytes=config.max_log_bytes,
        )
        if done.exit_code:
            raise RuntimeError("project_maintenance_patch_failed")
    baseline = live_baseline(spec)
    overlaid = []
    if spec.get("overlay_live_code"):
        for name, sha in baseline.items():
            item, rel = mapping(spec, name)
            if item.get("overlay_live_code") is False:
                continue
            # 只同步执行代码、提示模板和工作流；实际配置通过下面的字段白名单导出。
            if (
                Path(rel).suffix not in {".py", ".prompt"}
                and Path(rel).name != "_manifest.json"
                and not (name.startswith("workflows/") and name.endswith(".json"))
            ):
                continue
            source = target(Path(item["live"]), rel)
            data = source.read_bytes()
            if hashlib.sha256(data).hexdigest() != sha:
                raise ValueError("同步期间线上源码发生变化")
            if re.search(
                rb"gh[pousr]_[A-Za-z0-9]{25,}|\bsk-(?:proj-)?[A-Za-z0-9_-]{28,}|-----BEGIN .*PRIVATE KEY",
                data,
            ):
                raise ValueError("线上代码疑似含凭据，未同步")
            path = target(workspace, name)
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(data)
            path.chmod(source.stat().st_mode & 0o777)
            overlaid.append(name)
    context = workspace / ".project-context.json"
    if context.exists():
        raise ValueError("项目占用了配置快照保留路径")
    atomic_json(
        context,
        {
            "components": [
                {k: c[k] for k in ("mount", "commit")} for c in spec["components"]
            ],
            "effective_config": configuration_snapshot(spec),
        },
    )
    # 附加插件常在主仓 .gitignore 中；只强制加入明确的组件和脱敏快照。
    prefix = [
        "git",
        "-c",
        "core.hooksPath=/dev/null",
        "-c",
        "core.fsmonitor=false",
        "-c",
        "user.name=kkbk-dev-worker",
        "-c",
        "user.email=worker@localhost",
        "-c",
        "commit.gpgsign=false",
    ]
    commands = [
        prefix + ["add", "-A", "--", "."],
        prefix
        + [
            "add",
            "-f",
            "--",
            ".project-context.json",
            *[c["mount"] for c in spec["components"]],
            *overlaid,
        ],
        prefix + ["commit", "-qm", "1.0.0 组合开发基线"],
        prefix + ["rev-parse", "HEAD"],
    ]
    for index, command in enumerate(commands):
        done = run_process(
            sandbox_command(config, folder, command),
            cwd=workspace,
            env=agent_env(config, folder),
            stdout=folder / f"project-{index}.log",
            stderr=folder / f"project-{index}.err",
            timeout=config.prepare_timeout_sec,
            cancel=cancel,
            max_bytes=config.max_log_bytes,
        )
        if done.exit_code:
            raise RuntimeError("project_baseline_failed")
    base = (folder / "project-3.log").read_text().strip()
    from .source_release import dependency_pointer

    pointer = dependency_pointer(spec)
    atomic_json(
        folder / "project.json",
        {
            "spec": spec,
            "base_commit": base,
            "live": baseline,
            "overlaid_live_code": overlaid,
            "dependency_before": digest(pointer) if pointer else None,
        },
    )
    return base

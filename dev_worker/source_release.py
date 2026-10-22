"""源码候选封装、串行发布、健康检查和恢复；宿主策略不由候选代码提供。"""

from pathlib import Path
import hashlib
import json
import os
import shutil
import sys
import uuid

from . import project
from .process import run_process
from .store import atomic_json, exclusive_lock, now


class SourceReleaseError(RuntimeError):
    """候选、线上基线或健康检查不满足发布约定。"""


def save_candidate(config, task, workspace, result):
    """把验证过的字节复制到代理不可写的位置，固定文件、测试和来源版本。"""
    root_id = task["spec"].get("session_root", task["id"])
    origin = json.loads((config.tasks_dir / root_id / "project.json").read_text())
    spec = origin["spec"]
    folder = config.tasks_dir / task["id"] / "source-candidate"
    if folder.exists():
        raise SourceReleaseError("候选目录已经存在")
    folder.mkdir(mode=0o700)
    records, skipped = [], []
    for name in result["patch"]["files"]:
        project.relative(name)
        if (
            Path(name).name in {"README.md", "CHANGELOG.md", "LICENSE"}
            or project.is_test_file(name)
            or any(
                project.covers(name, prefix) for prefix in spec.get("non_runtime", [])
            )
        ):
            skipped.append(name)
            continue
        if Path(name).name in {
            "package.json",
            "package-lock.json",
            "pnpm-lock.yaml",
            "bun.lock",
            "Cargo.toml",
            "Cargo.lock",
        }:
            raise SourceReleaseError(
                "此发布策略尚未配置该生态的依赖与恢复步骤：" + name
            )
        item, rel = project.mapping(spec, name)
        source = project.target(workspace, name)
        sha = project.digest(source)
        data = source.read_bytes() if sha is not None else None
        if data is not None:
            destination = project.target(folder / "files", name)
            destination.parent.mkdir(parents=True, exist_ok=True)
            destination.write_bytes(data)
            destination.chmod(source.stat().st_mode & 0o777)
            if source.suffix == ".py":
                compile(data, name, "exec")
        records.append(
            {
                "name": name,
                "target": str(project.target(Path(item["live"]), rel)),
                "before": origin["live"].get(name),
                "after": sha,
                "mode": (source.stat().st_mode & 0o777) if data is not None else None,
                "service": item.get("service"),
                "core": not item.get("auto_publish", False),
            }
        )
    manifest = {
        "version": 1,
        "task_id": task["id"],
        "created_at": now(),
        "state": "verified",
        "base_commit": result.get("project_base_commit", task["spec"]["base_commit"]),
        "components": spec["components"],
        "patch_sha256": result["patch"]["sha256"],
        "test_exit": result["test_exit"],
        "checks": result.get("project_checks", []),
        "root_task_id": root_id,
        "files": records,
        "non_runtime": skipped,
        "policy": spec,
        "dependency_before": origin.get("dependency_before"),
    }
    atomic_json(folder / "candidate.json", manifest)
    return {
        "task_id": task["id"],
        "state": "verified",
        "files": len(records),
        "core_approval_required": any(r["core"] for r in records),
        "patch_sha256": manifest["patch_sha256"],
    }


def _command(config, command, folder, label, *, env=None):
    """仅运行宿主清单固定的命令数组，记录结果；不接受任务输出的 shell 字符串。"""
    done = run_process(
        command,
        cwd=folder,
        env=env or dict(os.environ),
        stdout=folder / (label + ".log"),
        stderr=folder / (label + ".err"),
        timeout=config.test_timeout_sec,
        max_bytes=config.max_log_bytes,
    )
    if done.exit_code:
        raise SourceReleaseError(label + "_failed")


def _write(path, data, mode=0o644):
    """在同目录替换普通文件，删除也经过显式路径检查。"""
    project.digest(path)
    if data is None:
        path.unlink(missing_ok=True)
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(".source-release-" + uuid.uuid4().hex)
    try:
        with temporary.open("xb") as stream:
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
        temporary.chmod(mode)
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def _services(config, policy, folder, services, operation):
    """停止、启动与健康检查只针对受影响的已配置服务，保持 QQ 登录服务不变。"""
    for name in sorted(services):
        if name in {"qq", "snowluma"}:
            raise SourceReleaseError("源码发布不能重启 QQ 或 SnowLuma 登录服务")
        service = policy.get("services", {}).get(name)
        if service is None or not service.get(operation):
            raise SourceReleaseError("缺少服务操作：" + name + "/" + operation)
        for index, command in enumerate(service[operation]):
            _command(config, command, folder, f"{operation}-{name}-{index}")


def _read_release(config, release_id):
    project.relative(release_id)
    if "/" in release_id:
        raise SourceReleaseError("发布编号无效")
    folder = config.root / "source-releases" / release_id
    if folder.is_symlink():
        raise SourceReleaseError("发布目录为符号链接")
    return folder, json.loads((folder / "manifest.json").read_text())


def _restore(config, folder, manifest):
    """恢复相同批次的旧文件和依赖指针，再启动服务并核验健康。"""
    manifest["state"] = "restoring"
    atomic_json(folder / "manifest.json", manifest)
    services = set(manifest["services"])
    _services(config, manifest["policy"], folder, services, "stop")
    for index, item in enumerate(manifest["files"]):
        path = Path(item["target"])
        if project.digest(path) not in {item["before"], item["after"]}:
            raise SourceReleaseError("恢复时线上文件已被外部修改，拒绝覆盖")
        data = (
            (folder / f"{index}.old").read_bytes()
            if item["before"] is not None
            else None
        )
        _write(path, data, item.get("before_mode") or 0o644)
        if project.digest(path) != item["before"]:
            raise SourceReleaseError("恢复后字节与备份不一致")
    _services(config, manifest["policy"], folder, services, "start")
    _services(config, manifest["policy"], folder, services, "health")
    manifest.update(state="rolled_back", restored_at=now())
    atomic_json(folder / "manifest.json", manifest)


def dependency_pointer(spec):
    dependency = spec.get("dependencies")
    if not dependency:
        return None
    return project.target(Path(dependency["site"]), "00-kkbk-source-release.pth")


def needs_dependencies(spec, names):
    """主项目或插件依赖清单变化时重建版本依赖；普通代码改动复用原环境。"""
    dependency = spec.get("dependencies")
    return bool(
        dependency
        and any(
            any(project.covers(name, p) for p in dependency["when"])
            or (
                name.startswith("plugins/")
                and name.endswith(("/requirements.txt", "/pyproject.toml"))
            )
            for name in names
        )
    )


def _dependencies(config, candidate_folder, candidate, folder):
    """依赖变化先在隔离目录安装并试运行，最后仅切换可恢复的路径指针。"""
    spec = candidate["policy"]
    dependency = spec.get("dependencies")
    if not needs_dependencies(spec, [r["name"] for r in candidate["files"]]):
        return None
    # 安装源来自已封装的候选完整工作树，而不是可继续修改的开发副本。
    source = folder / "trial-source"
    source.mkdir()
    # 只复制已跟踪普通文件；Git 命令与过滤器由现有隔离收集器执行，不在宿主运行候选 Git。
    inventory = json.loads((candidate_folder / "inventory.json").read_text())
    for item in inventory:
        origin = project.target(candidate_folder / "tree", item["name"])
        data = origin.read_bytes()
        if hashlib.sha256(data).hexdigest() != item["sha256"]:
            raise SourceReleaseError("完整候选字节已变化")
        path = project.target(source, item["name"])
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)
    venv = folder / "runtime-venv"
    _command(
        config,
        [config.system_python, "-m", "venv", str(venv)],
        folder,
        "dependency-venv",
    )
    python = venv / ("Scripts/python.exe" if sys.platform == "win32" else "bin/python")
    requirement = project.target(source, dependency["requirements"])
    if not requirement.is_file():
        raise SourceReleaseError("候选缺少依赖清单")
    requirements = [requirement, *sorted(source.glob("plugins/*/requirements.txt"))]
    for item in candidate["files"]:
        if item["name"].startswith("plugins/") and item["name"].endswith(
            "/pyproject.toml"
        ):
            if not project.target(
                source,
                str(Path(item["name"]).with_name("requirements.txt")).replace(
                    "\\", "/"
                ),
            ).is_file():
                raise SourceReleaseError("插件依赖变化需要同步 requirements.txt")
    command = [
        str(python),
        "-m",
        "pip",
        "install",
        "--disable-pip-version-check",
        "-r",
        str(requirement),
        "-i",
        config.pip_index_url,
    ]
    for path in requirements[1:]:
        command += ["-r", str(path)]
    if config.use_sandbox:
        # 允许安装时联网，但隐藏宿主资料，只写本次版本目录；不带 DSH 凭据。
        prefix = [
            "/usr/bin/bwrap",
            "--ro-bind",
            "/",
            "/",
            "--tmpfs",
            "/home",
            "--tmpfs",
            "/root",
            "--tmpfs",
            "/mnt",
            "--tmpfs",
            "/run",
            "--tmpfs",
            "/tmp",
            "--dev",
            "/dev",
            "--proc",
            "/proc",
            "--unshare-pid",
            "--die-with-parent",
            "--cap-drop",
            "ALL",
            "--bind",
            str(folder),
            str(folder),
            "--chdir",
            str(source),
            "--",
        ]
        command = prefix + command
    _command(
        config,
        command,
        folder,
        "dependency-install",
        env={
            "PATH": config.agent_path,
            "HOME": "/tmp",
            "LANG": "C.UTF-8",
            "PYTHONDONTWRITEBYTECODE": "1",
        },
    )
    # 试运行命令固定在宿主策略中，使用新依赖解释器且断网。
    for index, args in enumerate(dependency["checks"]):
        command = [str(python), *[arg.replace("{source}", str(source)) for arg in args]]
        if config.use_sandbox:
            command = prefix[:-1] + ["--unshare-net", "--", *command]
        _command(
            config,
            command,
            folder,
            f"dependency-check-{index}",
            env={
                "PATH": config.agent_path,
                "HOME": "/tmp",
                "LANG": "C.UTF-8",
                "PYTHONDONTWRITEBYTECODE": "1",
            },
        )
    sites = (
        list((venv / "lib").glob("python*/site-packages"))
        if os.name != "nt"
        else [venv / "Lib/site-packages"]
    )
    if len(sites) != 1:
        raise SourceReleaseError("版本依赖路径不唯一")
    pointer = dependency_pointer(spec)
    content = ("import sys; sys.path.insert(0, " + repr(str(sites[0])) + ")\n").encode()
    return {
        "name": "@dependency",
        "target": str(pointer),
        "before": candidate["dependency_before"],
        "after": hashlib.sha256(content).hexdigest(),
        "mode": 0o644,
        "core": True,
        "service": dependency["service"],
        "data": content,
    }


def seal_tree(config, folder, workspace, cancel):
    """依赖试运行需要完整源码；通过现有断网 Git 边界列清单，避免把未跟踪凭据打包。"""
    from .workspace import agent_env, sandbox_command

    output = folder / "source-inventory.log"
    command = [
        "git",
        "-c",
        "core.hooksPath=/dev/null",
        "-c",
        "core.fsmonitor=false",
        "ls-files",
        "-z",
    ]
    done = run_process(
        sandbox_command(config, workspace.parent, command),
        cwd=workspace,
        env=agent_env(config, workspace.parent),
        stdout=output,
        stderr=folder / "source-inventory.err",
        timeout=config.prepare_timeout_sec,
        cancel=cancel,
        max_bytes=config.max_log_bytes,
    )
    if done.exit_code:
        raise SourceReleaseError("无法封装完整候选")
    inventory = []
    for name in output.read_bytes().decode().split("\0"):
        if not name:
            continue
        path = project.target(workspace, name)
        sha = project.digest(path)
        if sha is None:
            continue
        destination = project.target(folder / "tree", name)
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_bytes(path.read_bytes())
        inventory.append({"name": name, "sha256": sha})
    atomic_json(folder / "inventory.json", inventory)


def publish(config, store, task_id, *, allow_core=False):
    """封装候选经基线检查后串行上线；异常与进程中断均留可恢复记录。"""
    with exclusive_lock(config.release_lock_path):
        for path in (config.root / "source-releases").glob("*/manifest.json"):
            old = json.loads(path.read_text())
            if old["state"] not in {"installed", "rolled_back", "rejected"}:
                raise SourceReleaseError("存在未恢复的发布：" + old["id"])
        task = store.get(task_id)
        if task["status"] != "succeeded" or task["cancel_requested"]:
            raise SourceReleaseError("任务未成功或已经取消")
        if task["result"].get("source_candidate", {}).get("state") != "verified":
            raise SourceReleaseError("没有可发布的源码封装候选")
        _, latest = store.session(task_id)
        if latest["id"] != task_id:
            raise SourceReleaseError("已有后续轮次，不能发布旧候选")
        candidate_folder = store.directory(task_id) / "source-candidate"
        candidate = json.loads((candidate_folder / "candidate.json").read_text())
        if (
            candidate["test_exit"] != 0
            or candidate["patch_sha256"] != task["result"]["patch"]["sha256"]
        ):
            raise SourceReleaseError("候选与独立测试不匹配")
        if any(r["core"] for r in candidate["files"]) and not allow_core:
            raise SourceReleaseError("核心候选需要维护端显式允许发布")
        if not candidate["files"]:
            raise SourceReleaseError("没有运行文件变化")
        # 候选身份固定后只读取封装字节，开发副本继续变化不会进入发布。
        records = candidate["files"]
        data = {}
        for item in records:
            item_mapping, rel = project.mapping(candidate["policy"], item["name"])
            if str(project.target(Path(item_mapping["live"]), rel)) != item["target"]:
                raise SourceReleaseError("候选映射发生变化")
            path = project.target(candidate_folder / "files", item["name"])
            if project.digest(path) != item["after"]:
                raise SourceReleaseError("候选字节发生变化")
            data[item["name"]] = (
                path.read_bytes() if item["after"] is not None else None
            )
            if project.digest(Path(item["target"])) != item["before"]:
                raise SourceReleaseError("线上基线发生变化：" + item["name"])
        release_id = task_id + "-" + uuid.uuid4().hex[:8]
        folder = config.root / "source-releases" / release_id
        folder.mkdir(parents=True, mode=0o700)
        manifest = {
            "id": release_id,
            "task_id": task_id,
            "state": "preparing",
            "created_at": now(),
            "files": records,
            "policy": candidate["policy"],
            "patch_sha256": candidate["patch_sha256"],
            "services": sorted({r["service"] for r in records if r["service"]}),
            "error": "",
        }
        save = lambda: atomic_json(folder / "manifest.json", manifest)
        # 发布 worker 本身时，停止/恢复命令不能依赖刚被候选覆盖的控制脚本。
        for service in manifest["policy"].get("services", {}).values():
            for commands in service.values():
                for command in commands:
                    if len(command) > 1 and Path(command[1]).name == "lifecycle.py":
                        control = folder / "lifecycle-control.py"
                        if not control.exists():
                            shutil.copyfile(command[1], control)
                        command[1] = str(control)
        save()
        touched = False
        try:
            dep = _dependencies(config, candidate_folder, candidate, folder)
            if dep:
                data[dep["name"]] = dep.pop("data")
                records.append(dep)
                manifest["services"] = sorted(
                    set(manifest["services"]) | {dep["service"]}
                )
            for index, item in enumerate(records):
                path = Path(item["target"])
                if project.digest(path) != item["before"]:
                    raise SourceReleaseError("安装前线上基线发生变化")
                if item["before"] is not None:
                    (folder / f"{index}.old").write_bytes(path.read_bytes())
                    item["before_mode"] = path.stat().st_mode & 0o777
                if data[item["name"]] is not None:
                    (folder / f"{index}.new").write_bytes(data[item["name"]])
            manifest["state"] = "stopping"
            save()
            touched = True
            _services(
                config, candidate["policy"], folder, set(manifest["services"]), "stop"
            )
            manifest["state"] = "installing"
            save()
            for item in records:
                if project.digest(Path(item["target"])) != item["before"]:
                    raise SourceReleaseError("停机期间线上文件发生变化")
                _write(Path(item["target"]), data[item["name"]], item["mode"] or 0o644)
                if project.digest(Path(item["target"])) != item["after"]:
                    raise SourceReleaseError("安装字节不一致")
            manifest["state"] = "verifying"
            save()
            _services(
                config, candidate["policy"], folder, set(manifest["services"]), "start"
            )
            _services(
                config, candidate["policy"], folder, set(manifest["services"]), "health"
            )
            manifest.update(
                state="installed", installed_at=now(), business_acceptance="pending"
            )
            save()
        except BaseException as exc:
            manifest["error"] = type(exc).__name__ + ": " + str(exc)[:256]
            if touched:
                try:
                    _restore(config, folder, manifest)
                except BaseException as again:
                    manifest.update(
                        state="restore_failed", restore_error=type(again).__name__
                    )
            else:
                manifest["state"] = "rejected"
            save()
            if not isinstance(exc, Exception):
                raise
        result = {k: manifest[k] for k in ("id", "task_id", "state", "error")}
        store.source_publication(task_id, result)
        return result


def rollback(config, release_id, *, recover=False):
    """维护端回退或恢复中断；普通回退不覆盖发布后人为修改的文件。"""
    with exclusive_lock(config.release_lock_path):
        folder, manifest = _read_release(config, release_id)
        if manifest["state"] == "rolled_back":
            from .store import Store

            Store(config.root).source_publication(
                manifest["task_id"], {"id": release_id, "state": "rolled_back"}
            )
            return {"id": release_id, "state": "rolled_back"}
        if not recover and manifest["state"] != "installed":
            raise SourceReleaseError("发布未完成，请显式使用恢复命令")
        if recover and manifest["state"] == "preparing":
            manifest["state"] = "rejected"
            atomic_json(folder / "manifest.json", manifest)
            return {"id": release_id, "state": "rejected"}
        for item in manifest["files"]:
            actual = project.digest(Path(item["target"]))
            expected = {item["before"], item["after"]} if recover else {item["after"]}
            if actual not in expected:
                raise SourceReleaseError("线上已发生其他修改，拒绝覆盖")
        _restore(config, folder, manifest)
        result = {"id": release_id, "state": manifest["state"]}
        from .store import Store

        Store(config.root).source_publication(manifest["task_id"], result)
        return result


def reconcile(config, store):
    """启动后恢复中断发布并修复磁盘/账本间的崩溃窗口，失败只记录一次而不无限重试。"""
    for path in sorted((config.root / "source-releases").glob("*/manifest.json")):
        manifest = json.loads(path.read_text())
        if manifest["state"] in {
            "preparing",
            "stopping",
            "installing",
            "verifying",
            "restoring",
        }:
            try:
                rollback(config, manifest["id"], recover=True)
            except Exception as exc:
                manifest = json.loads(path.read_text())
                manifest.update(
                    state="restore_failed", restore_error=type(exc).__name__
                )
                atomic_json(path, manifest)
        manifest = json.loads(path.read_text())
        record = {
            k: manifest.get(k)
            for k in ("id", "task_id", "state", "error", "restore_error")
        }
        if store.source_publication(manifest["task_id"]) != record:
            store.source_publication(manifest["task_id"], record)

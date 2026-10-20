"""工作流候选自动发布：绑定候选基线、备份、先目录后清单的串行安装、加载自检与恢复。

只发布 workflows/ 下的文件；源码与测试文件不进入线上。工作流按任务读取不可变快照，
发布无需重启服务：新任务读取新版，进行中的任务保持已取得的快照。
"""
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath
import hashlib
import json
import os
import shutil
import subprocess
import sys
import tempfile
import uuid

from .process import run_process
from .store import atomic_json, exclusive_lock
from .workspace import agent_env, sandbox_command

MANIFEST = 'enabled.json'
PREFIX = 'workflows/'
# 在独立解释器里执行线上加载器，读取结果与 bot 运行时完全一致。
_LOADER = r'''
import importlib.util, json, pathlib, sys
spec = importlib.util.spec_from_file_location("kkbk_live_workflows", sys.argv[1])
module = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = module
spec.loader.exec_module(module)
print(json.dumps(module.load_workflow_snapshot(pathlib.Path(sys.argv[2])).record(), ensure_ascii=False))
'''


class ReleaseError(RuntimeError):
    """发布前检查或发布后自检失败；不掩盖原因，原样写入发布记录。"""


def now():
    return datetime.now(timezone.utc).isoformat()


def tree(root: Path):
    """工作流目录的 {相对路径: sha256}；符号链接和特殊文件一律拒绝。"""
    if not root.is_dir() or root.is_symlink():
        raise ReleaseError(f'工作流目录不存在或为符号链接：{root}')
    result = {}
    for path in sorted(root.rglob('*')):
        rel = path.relative_to(root).as_posix()
        if path.is_symlink():
            raise ReleaseError('工作流目录含符号链接：' + rel)
        if path.is_dir():
            continue
        if not path.is_file():
            raise ReleaseError('工作流目录含非普通文件：' + rel)
        result[rel] = hashlib.sha256(path.read_bytes()).hexdigest()
    return result


def load_snapshot(loader: Path, root: Path):
    """返回加载器的快照记录；加载错误或启用集合为空都视为失败。"""
    done = subprocess.run([sys.executable, '-I', '-B', '-c', _LOADER, str(loader), str(root)],
                          capture_output=True, timeout=60)
    if done.returncode:
        raise ReleaseError('加载器执行失败：' + done.stderr.decode('utf-8', 'replace')[-300:])
    record = json.loads(done.stdout)
    if record['error']:
        raise ReleaseError('工作流加载失败：' + record['error'][:300])
    if not record['workflows']:
        raise ReleaseError('启用工作流为空')
    return record


def _target(root: Path, rel: str):
    relative = PurePosixPath(rel)
    if relative.is_absolute() or '..' in relative.parts or not relative.parts:
        raise ReleaseError('发布路径越界：' + rel)
    path = root.joinpath(*relative.parts)
    if not path.resolve().is_relative_to(root.resolve()):
        raise ReleaseError('发布路径越界：' + rel)
    return path


def _write(path: Path, data: bytes):
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, name = tempfile.mkstemp(dir=path.parent, prefix='.release-')
    try:
        with os.fdopen(fd, 'wb') as stream:
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
        os.chmod(name, 0o644)
        os.replace(name, path)
    finally:
        Path(name).unlink(missing_ok=True)


def apply_files(root: Path, writes, removals=()):
    """目录文件先写、启用清单最后切换，删除放在切换之后，读者不会看到引用缺失的清单。"""
    order = sorted(rel for rel in writes if rel != MANIFEST)
    if MANIFEST in writes:
        order.append(MANIFEST)
    for rel in order:
        _write(_target(root, rel), writes[rel])
    for rel in sorted(removals):
        _target(root, rel).unlink()


def restore(live: Path, backup: Path):
    """把线上目录恢复成备份内容，只改动有差异的文件。"""
    desired = {rel: _target(backup, rel).read_bytes() for rel in tree(backup)}
    current = tree(live)
    writes = {rel: data for rel, data in desired.items()
              if current.get(rel) != hashlib.sha256(data).hexdigest()}
    apply_files(live, writes, set(current) - set(desired))


def sync_workspace(config, folder: Path, cancel):
    """候选从线上当前工作流出发：覆盖工作副本 workflows/ 并提交，作为补丁基线。"""
    live = config.live_workflows
    baseline = tree(live)
    target = folder / 'ws' / 'workflows'
    if target.is_symlink():
        raise ReleaseError('工作副本 workflows 为符号链接')
    if target.exists():
        shutil.rmtree(target)
    shutil.copytree(live, target)
    if tree(target) != baseline:
        raise ReleaseError('同步期间线上工作流发生变化')
    git = '/usr/bin/git' if config.use_sandbox else 'git'
    prefix = [git, '-c', 'core.hooksPath=/dev/null', '-c', 'core.fsmonitor=false', '-c', 'commit.gpgsign=false',
              '-c', 'user.name=kkbk-dev-worker', '-c', 'user.email=worker@localhost']
    steps = [prefix + ['add', '-A', '--', 'workflows'],
             prefix + ['commit', '-q', '--allow-empty', '-m', 'sync live workflows'],
             prefix + ['rev-parse', 'HEAD']]
    for index, command in enumerate(steps):
        result = run_process(sandbox_command(config, folder, command), cwd=folder / 'ws', env=agent_env(config, folder),
                             stdout=folder / f'sync-{index}.log', stderr=folder / f'sync-{index}.err',
                             timeout=config.prepare_timeout_sec, cancel=cancel, max_bytes=config.max_log_bytes)
        if result.exit_code:
            raise RuntimeError(f'sync_step_{index}_failed')
    head = (folder / 'sync-2.log').read_text(encoding='utf-8').strip()
    if len(head) != 40:
        raise RuntimeError('无法确定候选基线提交')
    return baseline, head


def summary(manifest):
    after = manifest.get('after') or {}
    return {'release_id': manifest['id'], 'task_id': manifest.get('task_id'), 'state': manifest['state'],
            'workflows': [f"{item['id']}@{item['version']}" for item in after.get('workflows', [])],
            'files': manifest.get('files', []), 'error': manifest.get('error', ''),
            'restore_error': manifest.get('restore_error', '')}


def _new_release(config, task_id):
    stamp = datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%S%fZ')
    folder = config.releases_dir / f'{stamp}-{uuid.uuid4().hex[:8]}'
    folder.mkdir(parents=True, mode=0o700)
    return folder


def publish(config, *, task_id, workspace: Path, files, baseline):
    """已通过测试的工作流候选直接上线；自检失败自动恢复。只返回结果，不抛出普通异常。"""
    names = [name for name in files if name.startswith(PREFIX)]
    if not names:
        return {'release_id': None, 'task_id': task_id, 'state': 'skipped', 'workflows': [], 'files': [],
                'error': '补丁没有 workflows/ 文件', 'restore_error': ''}
    live = config.live_workflows
    try:
        with exclusive_lock(config.release_lock_path):
            folder = _new_release(config, task_id)
            manifest = {'id': folder.name, 'task_id': task_id, 'state': 'checking', 'created_at': now(),
                        'files': names, 'live': str(live)}
            save = lambda: atomic_json(folder / 'manifest.json', manifest)
            save()
            try:
                current = tree(live)
                if current != baseline:
                    raise ReleaseError('线上工作流在候选生成后已变化，未自动合并')
                changes = {}
                for name in names:
                    source = _target(workspace / 'workflows', name[len(PREFIX):])
                    if source.is_symlink() or not source.is_file():
                        raise ReleaseError('不支持自动发布删除或非普通文件：' + name)
                    changes[name[len(PREFIX):]] = source.read_bytes()
                staging = folder / 'candidate'
                shutil.copytree(live, staging)
                apply_files(staging, changes)
                expected = load_snapshot(config.live_loader, staging)
                shutil.copytree(live, folder / 'before')
                manifest.update(state='installing', before_tree=current)
                save()
                apply_files(live, changes)
                manifest['state'] = 'verifying'
                save()
                actual = load_snapshot(config.live_loader, live)
                if actual != expected:
                    raise ReleaseError('线上加载结果与候选不一致')
                manifest.update(state='installed', after=actual, after_tree=tree(live), installed_at=now())
                save()
            except BaseException as exc:
                manifest['error'] = type(exc).__name__ + ': ' + str(exc)[:300]
                if manifest['state'] in ('installing', 'verifying'):
                    try:
                        restore(live, folder / 'before')
                        if tree(live) != manifest['before_tree']:
                            raise ReleaseError('恢复后内容与备份不一致')
                        manifest['state'] = 'rolled_back'
                    except Exception as again:
                        manifest.update(state='restore_failed',
                                        restore_error=type(again).__name__ + ': ' + str(again)[:300])
                else:
                    manifest['state'] = 'failed'
                save()
                if not isinstance(exc, Exception):
                    raise
            return summary(manifest)
    except BlockingIOError:
        return {'release_id': None, 'task_id': task_id, 'state': 'failed', 'workflows': [], 'files': names,
                'error': '另一个发布或回退正在进行', 'restore_error': ''}


def releases(config):
    if not config.releases_dir.exists():
        return []
    items = []
    for folder in sorted(config.releases_dir.iterdir(), reverse=True):
        path = folder / 'manifest.json'
        if path.is_file():
            items.append((folder, json.loads(path.read_text(encoding='utf-8'))))
    return items


def rollback(config):
    """回退最近一次仍生效的自动发布；线上之后又被改过则拒绝，避免覆盖未知改动。"""
    if config.live_workflows is None:
        raise ValueError('未配置线上工作流目录')
    with exclusive_lock(config.release_lock_path):
        installed = [(folder, item) for folder, item in releases(config) if item['state'] == 'installed']
        if not installed:
            raise ValueError('没有可回退的自动发布')
        folder, manifest = installed[0]
        live = config.live_workflows
        if tree(live) != manifest['after_tree']:
            raise ReleaseError('线上工作流在该次发布之后又有改动，拒绝自动回退')
        restore(live, folder / 'before')
        if tree(live) != manifest['before_tree']:
            raise ReleaseError('回退后内容与发布前备份不一致')
        manifest.update(state='reverted', reverted_at=now())
        try:
            record = load_snapshot(config.live_loader, live)
            manifest['reverted_to'] = [f"{item['id']}@{item['version']}" for item in record['workflows']]
        except ReleaseError as exc:
            manifest['reverted_check_error'] = str(exc)[:300]
        atomic_json(folder / 'manifest.json', manifest)
        return {**summary(manifest), 'reverted_to': manifest.get('reverted_to', []),
                'check_error': manifest.get('reverted_check_error', '')}

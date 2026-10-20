"""工作流候选自动发布：基线绑定、清单最后切换、自检恢复、回退与 worker 接线回归。"""
from dataclasses import replace
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch
import json
import os
import shutil
import subprocess
import sys
import unittest

from dev_worker import release
from dev_worker.config import WorkerConfig
from dev_worker.coordinator import Coordinator
from dev_worker.store import exclusive_lock
from dev_worker.worker import Worker

REPO = Path(__file__).resolve().parents[1]
SHIPPED = REPO / 'MaiBot' / 'workflows'
LOADER = REPO / 'MaiBot' / 'src' / 'kkbk_engine' / 'workflows.py'
WORKFLOW = 'catalog/conservative-web-research'


def bump(root: Path, version='1.1.0'):
    """在 root 下生成同一工作流的新版本，并把启用清单切过去。"""
    value = json.loads((root / WORKFLOW / '1.0.0.json').read_text(encoding='utf-8'))
    value['version'] = version
    (root / WORKFLOW / f'{version}.json').write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding='utf-8')
    manifest = {'schema_version': 1, 'enabled': [
        {'id': 'conservative-web-research', 'version': version, 'file': f'{WORKFLOW}/{version}.json'}]}
    (root / 'enabled.json').write_text(json.dumps(manifest, indent=2), encoding='utf-8')
    return ['workflows/' + WORKFLOW + f'/{version}.json', 'workflows/enabled.json']


class ReleaseTests(unittest.TestCase):
    def setUp(self):
        self.temp = TemporaryDirectory(); self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.live = self.root / 'live' / 'workflows'
        shutil.copytree(SHIPPED, self.live)
        self.config = WorkerConfig(root=self.root / 'worker', source_repo=self.root / 'source',
                                   use_sandbox=False, live_workflows=self.live, live_loader=LOADER)
        self.workspace = self.root / 'ws'
        shutil.copytree(self.live, self.workspace / 'workflows')
        self.original = release.tree(self.live)

    def publish(self, files):
        return release.publish(self.config, task_id='task', workspace=self.workspace,
                               files=files, baseline=self.original)

    def test_publish_installs_candidate_and_keeps_backup(self):
        result = self.publish(bump(self.workspace / 'workflows'))
        self.assertEqual(result['state'], 'installed', result)
        self.assertEqual(result['workflows'], ['conservative-web-research@1.1.0'])
        self.assertEqual(release.tree(self.live), release.tree(self.workspace / 'workflows'))
        folder = self.config.releases_dir / result['release_id']
        self.assertEqual(release.tree(folder / 'before'), self.original)
        manifest = json.loads((folder / 'manifest.json').read_text(encoding='utf-8'))
        self.assertEqual(manifest['after_tree'], release.tree(self.live))

    def test_non_workflow_patch_is_skipped(self):
        result = self.publish(['pytests/kkbk_engine/test_new.py'])
        self.assertEqual(result['state'], 'skipped')
        self.assertFalse(self.config.releases_dir.exists())

    def test_live_change_after_candidate_is_refused(self):
        files = bump(self.workspace / 'workflows')
        (self.live / WORKFLOW / 'manual.json').write_text('{}', encoding='utf-8')
        changed = release.tree(self.live)
        result = self.publish(files)
        self.assertEqual(result['state'], 'failed')
        self.assertIn('已变化', result['error'])
        self.assertEqual(release.tree(self.live), changed)

    def test_invalid_candidate_never_touches_live(self):
        files = bump(self.workspace / 'workflows')
        (self.workspace / 'workflows' / WORKFLOW / '1.1.0.json').write_text('{"broken"', encoding='utf-8')
        result = self.publish(files)
        self.assertEqual(result['state'], 'failed')
        self.assertIn('工作流加载失败', result['error'])
        self.assertEqual(release.tree(self.live), self.original)

    def test_self_check_mismatch_restores_live(self):
        files = bump(self.workspace / 'workflows')
        real = release.load_snapshot
        calls = []
        def loader(path, root):
            calls.append(root)
            record = real(path, root)
            return record if len(calls) == 1 else {**record, 'manifest_sha256': 'different'}
        with patch('dev_worker.release.load_snapshot', side_effect=loader):
            result = self.publish(files)
        self.assertEqual(result['state'], 'rolled_back', result)
        self.assertIn('不一致', result['error'])
        self.assertEqual(release.tree(self.live), self.original)

    def test_manifest_is_written_after_catalog_files(self):
        order = []
        real = release._write
        def write(path, data):
            order.append(path.name); real(path, data)
        with patch('dev_worker.release._write', side_effect=write):
            release.apply_files(self.live, {'enabled.json': b'{}', 'catalog/x/2.0.0.json': b'{}',
                                            'catalog/a/1.0.0.json': b'{}'})
        self.assertEqual(order[-1], 'enabled.json')

    def test_rollback_restores_latest_release_once(self):
        result = self.publish(bump(self.workspace / 'workflows'))
        reverted = release.rollback(self.config)
        self.assertEqual(reverted['state'], 'reverted')
        self.assertEqual(reverted['reverted_to'], ['conservative-web-research@1.0.0'])
        self.assertEqual(release.tree(self.live), self.original)
        self.assertEqual(reverted['release_id'], result['release_id'])
        with self.assertRaises(ValueError):
            release.rollback(self.config)

    def test_rollback_refuses_after_later_live_change(self):
        self.publish(bump(self.workspace / 'workflows'))
        (self.live / WORKFLOW / 'manual.json').write_text('{}', encoding='utf-8')
        with self.assertRaises(release.ReleaseError):
            release.rollback(self.config)
        self.assertTrue((self.live / WORKFLOW / 'manual.json').exists())

    @unittest.skipIf(os.name == 'nt', 'Windows 文件锁同进程语义不同，Linux 在 WSL 验收')
    def test_concurrent_release_is_reported(self):
        files = bump(self.workspace / 'workflows')
        with exclusive_lock(self.config.release_lock_path):
            result = self.publish(files)
        self.assertEqual(result['state'], 'failed')
        self.assertEqual(release.tree(self.live), self.original)

    def test_rollback_operation_requires_admin_origin(self):
        source = self.root / 'source'; source.mkdir()
        for args in [('init', '-q'), ('config', 'user.name', 'Fixture'), ('config', 'user.email', 'f@localhost')]:
            subprocess.run(['git', '-C', str(source), *args], check=True, capture_output=True)
        coordinator = Coordinator(Worker(self.config))
        origin = {'platform': 'qq', 'session_id': 's', 'group_id': '1022382380', 'message_id': 'm',
                  'user_id': 'stranger', 'text': '回退'}
        with self.assertRaises(PermissionError):
            coordinator.dispatch({'version': 1, 'operation': 'rollback', 'origin': origin, 'payload': {}})
        with self.assertRaises(ValueError):
            coordinator.dispatch({'version': 1, 'operation': 'rollback',
                                  'origin': {**origin, 'user_id': '1020050112'}, 'payload': {}})


class WorkerReleaseTests(unittest.TestCase):
    def setUp(self):
        self.temp = TemporaryDirectory(); self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.source = self.root / 'source'; self.source.mkdir()
        for args in [('init', '-q'), ('config', 'user.name', 'Fixture'), ('config', 'user.email', 'f@localhost')]:
            subprocess.run(['git', '-C', str(self.source), *args], check=True, capture_output=True)
        # 源码里的工作流是旧版；候选必须以线上当前版本为基线。
        (self.source / 'workflows').mkdir()
        (self.source / 'workflows' / 'enabled.json').write_text('{"stale": true}', encoding='utf-8')
        subprocess.run(['git', '-C', str(self.source), 'add', '.'], check=True, capture_output=True)
        subprocess.run(['git', '-C', str(self.source), 'commit', '-qm', 'fixture'], check=True, capture_output=True)
        self.live = self.root / 'live' / 'workflows'
        shutil.copytree(SHIPPED, self.live)
        self.config = WorkerConfig(root=self.root / 'worker', source_repo=self.source, base_ref='HEAD',
                                   use_sandbox=False, agent_path=os.environ['PATH'], prepare_timeout_sec=15,
                                   agent_timeout_sec=15, test_timeout_sec=15,
                                   live_workflows=self.live, live_loader=LOADER)

    def run_task(self, scope):
        worker = Worker(self.config)
        task = worker.submit('1020050112', 'bump workflow', scope=scope)
        folder = worker.store.directory(task['id'])
        script = self.root / 'agent.py'
        # 代码修改任务的工作副本里只有源码旧版工作流，改一个普通文件即可。
        action = 'bump(Path("workflows"))' if scope == 'persistent_workflow' else 'Path("new.txt").write_text("x")'
        script.write_text(
            'import json,sys\nsys.path[:0]=[%r,%r]\nfrom test_dev_release import bump\n'
            'from pathlib import Path\n%s\n'
            'print(json.dumps({"type":"final","text":"done"}))\n'
            % (str(REPO), str(Path(__file__).parent), action), encoding='utf-8')
        def prepared(*args):
            (folder / 'venv').mkdir(); (folder / 'agent-home').mkdir()
        with patch('dev_worker.worker.prepare_environment', side_effect=prepared), \
             patch('dev_worker.worker.agent_command', return_value=[sys.executable, str(script)]), \
             patch('dev_worker.worker.test_command', return_value=[sys.executable, '-c', 'pass']):
            return worker.run_once()

    def test_verified_workflow_candidate_is_published_from_live_baseline(self):
        result = self.run_task('persistent_workflow')
        self.assertEqual(result['status'], 'succeeded', result)
        self.assertEqual(sorted(result['result']['patch']['files']),
                         ['workflows/' + WORKFLOW + '/1.1.0.json', 'workflows/enabled.json'])
        self.assertEqual(result['result']['release']['state'], 'installed', result['result'])
        self.assertTrue((self.live / WORKFLOW / '1.1.0.json').is_file())
        projected = Coordinator(Worker(self.config)).project(result)
        self.assertFalse(projected['candidate_only'])

    def test_code_change_is_never_published(self):
        result = self.run_task('code_change')
        self.assertEqual(result['status'], 'succeeded', result)
        self.assertNotIn('release', result['result'])
        self.assertEqual(release.tree(self.live), release.tree(SHIPPED))

    def test_disabled_release_keeps_candidate_only(self):
        self.config = replace(self.config, live_workflows=None)
        result = self.run_task('persistent_workflow')
        self.assertNotIn('release', result['result'])
        self.assertEqual(release.tree(self.live), release.tree(SHIPPED))


if __name__ == '__main__':
    unittest.main()

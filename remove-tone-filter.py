"""移除旧语气过滤 Hook，保留任务识图及宿主的完整原生历史。"""
import ast
from datetime import datetime, timezone
import json
from pathlib import Path
import shutil


ROOT = Path('/home/qqbot/QQbot/MaiBot')
LOCAL = Path('/mnt/d/code/web/QQbot/MaiBot')


def remove_hooks(source):
    """只删除确定的语气 Hook 和调用，遇到其他实现停止覆盖。"""
    tree = ast.parse(source)
    lines = source.splitlines(keepends=True)
    ranges = []
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and node.module == 'tone_context':
            ranges.append((node.lineno, node.end_lineno))
        elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name == 'tone_before':
            start = min([node.lineno, *[d.lineno for d in node.decorator_list]])
            ranges.append((start, node.end_lineno))
    updated = ''.join(line for i, line in enumerate(lines, 1)
                      if not any(start <= i <= end for start, end in ranges))
    old = 'await self.flow.before(self.ctx, planner_context(kwargs))'
    if old in updated:
        if updated.count(old) != 1:
            raise RuntimeError('规划器语气过滤调用不唯一')
        updated = updated.replace(old, 'await self.flow.before(self.ctx, kwargs)', 1)
    if any(name in updated for name in ['planner_context(', 'reply_context(', 'tone_context', 'isolate_old_tone']):
        raise RuntimeError('存在未识别的语气过滤实现')
    ast.parse(updated)
    return updated


def apply(root):
    """精确备份并移除语气模块，重复部署也不会重新注册旧 Hook。"""
    folder = root / 'plugins/qqbot-task-assist'
    plugin = folder / 'plugin.py'
    if not plugin.exists():
        raise RuntimeError('未找到任务与引用识图插件')
    before = plugin.read_text(encoding='utf-8-sig')
    after = remove_hooks(before)
    manifest = folder / '_manifest.json'
    manifest_before = manifest.read_text(encoding='utf-8-sig')
    manifest_data = json.loads(manifest_before)
    if manifest_data.get('version') == '1.0.3':
        manifest_data['version'] = '1.0.4'
    manifest_after = json.dumps(manifest_data, ensure_ascii=False, indent=2) + '\n'
    readme = folder / 'README.md'
    readme_before = readme.read_text(encoding='utf-8-sig')
    note = '1.0.4 移除旧语气历史过滤和回复改写 Hook；完整保留正文、原生思考及工具调用链。语气遵循现有人设，任务优先、真实 @ 与引用识图保持启用。'
    readme_after = readme_before if note in readme_before else readme_before.rstrip() + '\n\n' + note + '\n'
    retired = [folder / name for name in ['tone_context.py', 'test_tone_context.py']]
    changed = (before != after or manifest_before != manifest_after
               or readme_before != readme_after or any(p.exists() for p in retired))
    if changed:
        stamp = datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%S%fZ')
        backup = root / 'data/tone-filter-backups' / stamp
        backup.mkdir(parents=True, mode=0o700)
        for path in [plugin, manifest, readme, *retired]:
            if path.exists():
                # Windows 挂载目录可能不允许复制时间戳，备份只需要精确保留内容。
                shutil.copyfile(path, backup / path.name)
        plugin.write_text(after, encoding='utf-8')
        manifest.write_text(manifest_after, encoding='utf-8')
        readme.write_text(readme_after, encoding='utf-8')
        for path in retired:
            path.unlink(missing_ok=True)
        print(json.dumps({'root': str(root), 'removed_tone_filter': True, 'backup': str(backup)}, ensure_ascii=False))
    else:
        print(json.dumps({'root': str(root), 'removed_tone_filter': True, 'already_applied': True}, ensure_ascii=False))


if __name__ == '__main__':
    # 本地部署源和实际运行目录都处理，防止下次部署把旧过滤重新带回来。
    for root in [LOCAL, ROOT]:
        apply(root)

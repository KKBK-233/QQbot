"""逐文件接入新引擎；保留生产配置、登录、现有插件和其他未提交补丁。"""

from pathlib import Path
import argparse
import hashlib
import json
import shutil
import time

SOURCE = Path('/mnt/d/code/web/QQbot')
TARGET = Path('/home/qqbot/QQbot/MaiBot')
BASELINE = Path('/mnt/d/code/web/sandbox-QQbot/MaiBot')
STAGE = Path('/home/qqbot/QQbot/kkbk-engine-stage')


def digest(path):
    """只忽略已有部署的换行差异，不忽略任何源码变化。"""
    return hashlib.sha256(path.read_bytes().replace(b'\r\n', b'\n')).hexdigest()


def file_map():
    """只部署本次实现的文件，插件实现的其余部分保留线上版本。"""
    result = {f'src/kkbk_engine/{p.name}': p for p in (SOURCE / 'MaiBot/src/kkbk_engine').glob('*.py')}
    for name in ('src/chat/heart_flow/heartflow_manager.py', 'src/plugin_runtime/hook_catalog.py',
                 'plugins/qqbot-task-assist/plugin.py'):
        result[name] = SOURCE / 'MaiBot' / name
    result['plugins/qqbot-image/plugin.py'] = SOURCE / 'image-tool/maibot-plugin/plugin.py'
    return result


def main():
    """先检查运行快照一致性，再暂存或逐文件部署并保存原文件。"""
    parser = argparse.ArgumentParser()
    parser.add_argument('mode', choices=['stage', 'deploy'])
    args = parser.parse_args()
    files = file_map()
    existing = [name for name in files if not name.startswith('src/kkbk_engine/')]
    mismatches = [name for name in existing if digest(TARGET / name) != digest(BASELINE / name)]
    if args.mode == 'stage' and mismatches:
        raise SystemExit('运行源码已偏离基线，先合并：' + ', '.join(mismatches))
    destination = STAGE if args.mode == 'stage' else TARGET
    backup = SOURCE / 'data/kkbk-cutover' / time.strftime('%Y%m%dT%H%M%SZ', time.gmtime())
    if args.mode == 'deploy':
        backup.mkdir(parents=True, exist_ok=False)
    manifest = []
    for name, source in files.items():
        target = destination / name
        if args.mode == 'deploy':
            staged = STAGE / name
            if not staged.exists() or digest(staged) != digest(source):
                raise SystemExit('暂存后源码发生变化，请重新暂存：' + name)
            baseline = BASELINE / name
            if name in existing and digest(target) != digest(baseline):
                raise SystemExit('生产文件在检查后发生变化：' + name)
            if target.exists():
                saved = backup / name
                saved.parent.mkdir(parents=True, exist_ok=True)
                # Windows 挂载盘不支持部分 Linux 元数据，备份只复制原始字节。
                shutil.copyfile(target, saved)
        target.parent.mkdir(parents=True, exist_ok=True)
        # 保真复制，生产旧文件有换行噪音也不影响其他文件。
        shutil.copy2(source, target)
        assert source.read_bytes() == target.read_bytes()
        manifest.append({'path': name, 'sha256': hashlib.sha256(target.read_bytes()).hexdigest()})
    report = {'mode': args.mode, 'files': manifest}
    if args.mode == 'deploy':
        (backup / 'manifest.json').write_text(json.dumps(report, indent=2), encoding='utf-8')
        report['backup'] = str(backup)
    print(json.dumps(report, ensure_ascii=False))


if __name__ == '__main__':
    main()

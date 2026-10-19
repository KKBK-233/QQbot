"""下架离线 Python 插件并撤销固定提权入口，归档文件与配额数据。"""
from datetime import datetime, timezone
from pathlib import Path
import argparse
import ast
import json
import os
import shutil
import time
import urllib.request

ROOT = Path('/home/qqbot/QQbot')
PLUGIN_ID = 'local.qqbot-python-sandbox'

def api(path, method='GET'):
    """凭据仅用于本机管理请求，不写进日志或归档清单。"""
    token = json.loads((ROOT/'MaiBot/data/webui.json').read_text())['access_token']
    request = urllib.request.Request('http://127.0.0.1:8001/api/webui/plugins/'+path,
        headers={'Cookie': 'maibot_session='+token}, method=method)
    with urllib.request.urlopen(request, timeout=30) as response:
        return json.load(response)

def main():
    """先确认无沙箱任务，再禁用插件、撤销入口并保存可追溯备份。"""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--apply', action='store_true')
    args = parser.parse_args()
    if os.geteuid() != 0:
        raise SystemExit('请在 WSL 内以 root 运行，才能撤销固定 sudo 入口')
    group = Path('/sys/fs/cgroup/qqbot-python-sandbox/cgroup.procs')
    if group.exists() and group.read_text().strip():
        raise SystemExit('沙箱仍有运行进程，请任务结束后重试')
    plugin = ROOT/'MaiBot/plugins/qqbot-python-sandbox'
    if plugin.exists():
        manifest = json.loads((plugin/'_manifest.json').read_text())
        if manifest.get('id') != PLUGIN_ID:
            raise SystemExit('插件目录身份不符，停止处理')
    if not args.apply:
        print(json.dumps({'apply': False, 'plugin_installed': plugin.exists()}, ensure_ascii=False))
        return

    # 主机路径固定；保留源文件、审计数据和系统依赖，不删除共享软件包或账户。
    backup = ROOT/'retired-components'/('python-sandbox-'+datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ'))
    backup.mkdir(parents=True, mode=0o700)
    installed = api('installed')['plugins']
    state = next((p for p in installed if p.get('id') == PLUGIN_ID), None)
    if state and state.get('enabled'):
        result = api('config/'+PLUGIN_ID+'/toggle', 'POST')
        if not result.get('success') or result.get('enabled'):
            raise RuntimeError('运行时禁用失败，停止后续归档')
    for _ in range(10):
        state = next((p for p in api('installed')['plugins'] if p.get('id') == PLUGIN_ID), None)
        if not state or not state.get('loaded'):
            break
        time.sleep(0.5)
    else:
        raise RuntimeError('插件仍在运行，停止后续归档')
    if group.exists() and group.read_text().strip():
        raise RuntimeError('插件禁用后仍存在沙箱进程，停止归档')

    archived=[]
    sources=[plugin, Path('/etc/sudoers.d/qqbot-python-sandbox'),
        Path('/usr/local/libexec/qqbot-python-launch'), Path('/usr/local/libexec/qqbot-python-kill'),
        Path('/usr/local/share/qqbot-python-sandbox/worker.py')]
    for source in sources:
        if source.exists():
            destination=backup/'files'/str(source).lstrip('/')
            destination.parent.mkdir(parents=True, exist_ok=True)
            shutil.move(str(source), str(destination))
            archived.append(str(source))

    # 仅移除生产源码中的两个入口，不部署尚未上线的工作流代码。
    changed=[]
    edits=[(ROOT/'MaiBot/src/kkbk_engine/runtime.py', ', "run_python"', ''),
           (ROOT/'MaiBot/src/kkbk_engine/prompt.py', '、识图或运行 Python 时', '或识图时')]
    for target, old, new in edits:
        before=target.read_text()
        if old not in before:
            continue
        if before.count(old) != 1:
            raise RuntimeError('源码锚点不唯一：'+target.name)
        after=before.replace(old,new)
        ast.parse(after)
        destination=backup/'source'/target.name
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(target,destination)
        target.write_text(after)
        changed.append(str(target.relative_to(ROOT)))
    receipt={'plugin_removed':not plugin.exists(),'archived':archived,'source_changed':changed,
             'budget_data_preserved':True,'shared_packages_preserved':True}
    (backup/'receipt.json').write_text(json.dumps(receipt,ensure_ascii=False,indent=2))
    print(json.dumps({'backup':str(backup),**receipt},ensure_ascii=False))

if __name__=='__main__':
    main()

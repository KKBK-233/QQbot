"""按明确文件集合更新工作流或开发入口，先备份并校验，保留其余运行文件。"""
from pathlib import Path
import argparse
import hashlib
import json
import os
import shutil
import time

SOURCE = Path('/mnt/d/code/web/QQbot')
TARGET = Path('/home/qqbot/QQbot/MaiBot')
DEV = Path('/home/qqbot/dev-harness')


def sha(path):
    return hashlib.sha256(path.read_bytes()).hexdigest() if path.exists() else None


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('phase',choices=['workflows','development','task-resume','forward-images','snowluma-adapter','action-protocol','development-session'])
    args=parser.parse_args()
    # 会话控制只更新开发工具与 worker，不连带发布检索候选或覆盖线上工作流。
    if args.phase=='development-session':
        names=['src/kkbk_engine/development.py','src/kkbk_engine/dev_protocol.py','src/kkbk_engine/prompt.py']
        files=[(SOURCE/'MaiBot'/name,TARGET/name) for name in names]
        files += [(p,DEV/'worker-code/dev_worker'/p.name) for p in (SOURCE/'dev_worker').glob('*.py')]
        return install(args.phase,files)
    # 动作协议修复独立部署，避免连带发布尚未验收的运行时及检索候选。
    if args.phase=='action-protocol':
        names=['src/kkbk_engine/actions.py','src/kkbk_engine/prompt.py']
        return install(args.phase,[(SOURCE/'MaiBot'/name,TARGET/name) for name in names])
    # 适配器入站媒体预算只改适配器两个文件，不连带更新引擎运行时。
    if args.phase=='snowluma-adapter':
        adapter='plugins/MaiBot-SnowLuma-Adapter/codecs/inbound/'
        names=[adapter+'message_codec.py',adapter+'cards.py']
        return install(args.phase,[(SOURCE/'MaiBot'/name,TARGET/name) for name in names])
    # 运行时的直接依赖一起更新，避免只复制 runtime 后与旧预算或工具协议不兼容。
    names=['src/kkbk_engine/runtime.py','src/kkbk_engine/workflows.py','src/kkbk_engine/config.py',
           'src/kkbk_engine/builtin_tools.py','src/kkbk_engine/guards.py','src/kkbk_engine/search_evidence.py',
           'src/kkbk_engine/forward.py']
    if args.phase=='development':
        names+=['src/kkbk_engine/development.py','src/kkbk_engine/dev_protocol.py',
                'src/kkbk_engine/teaching.py','src/kkbk_engine/prompt.py']
    files=[(SOURCE/'MaiBot'/name,TARGET/name) for name in names]
    if args.phase=='forward-images':
        files += [(SOURCE/'MaiBot/plugins/qqbot-task-assist/plugin.py',TARGET/'plugins/qqbot-task-assist/plugin.py'),
                  (SOURCE/'image-tool/maibot-plugin/forward_vision.py',TARGET/'plugins/qqbot-task-assist/forward_vision.py')]
    if args.phase=='workflows':
        files += [(p,TARGET/p.relative_to(SOURCE/'MaiBot'))
                  for p in (SOURCE/'MaiBot/workflows').rglob('*.json')]
    elif args.phase=='development':
        files += [(p,DEV/'worker-code/dev_worker'/p.name) for p in (SOURCE/'dev_worker').glob('*.py')]
    install(args.phase,files)


def install(phase,files):
    stamp=time.strftime('%Y%m%dT%H%M%SZ',time.gmtime())+'-'+str(os.getpid())
    release=DEV/'releases'/stamp
    release.mkdir(parents=True,exist_ok=False,mode=0o700)
    records=[]
    # 所有字节先暂存并编译；写入前统一验证线上文件没有发生变化。
    for index,(source,target) in enumerate(files):
        raw=source.read_bytes()
        if source.suffix=='.py': compile(raw,str(source),'exec')
        (release/f'{index}.new').write_bytes(raw)
        old=sha(target)
        if old is not None: shutil.copyfile(target,release/f'{index}.old')
        records.append({'source':str(source),'target':str(target),'before':old,
                        'after':hashlib.sha256(raw).hexdigest(),'index':index})
    assert all(sha(Path(item['target']))==item['before'] for item in records),'线上文件发生变化'
    manifest={'phase':phase,'files':records,'state':'prepared'}
    (release/'manifest.json').write_text(json.dumps(manifest,indent=2))
    for item in records:
        target=Path(item['target']); target.parent.mkdir(parents=True,exist_ok=True)
        temporary=target.with_name(target.name+'.harness-new')
        temporary.write_bytes((release/f"{item['index']}.new").read_bytes())
        temporary.replace(target)
        assert sha(target)==item['after']
    manifest['state']='installed'
    (release/'manifest.json').write_text(json.dumps(manifest,indent=2))
    print(json.dumps({'phase':phase,'release':str(release),'files':len(records)}))


if __name__=='__main__': main()

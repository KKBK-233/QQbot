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
    parser.add_argument('phase',choices=['workflows','development'])
    args=parser.parse_args()
    names=['src/kkbk_engine/runtime.py','src/kkbk_engine/workflows.py']
    if args.phase=='development':
        names+=['src/kkbk_engine/development.py','src/kkbk_engine/dev_protocol.py']
    files=[(SOURCE/'MaiBot'/name,TARGET/name) for name in names]
    if args.phase=='workflows':
        files += [(p,TARGET/p.relative_to(SOURCE/'MaiBot'))
                  for p in (SOURCE/'MaiBot/workflows').rglob('*.json')]
    else:
        files += [(p,DEV/'worker-code/dev_worker'/p.name) for p in (SOURCE/'dev_worker').glob('*.py')]
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
    manifest={'phase':args.phase,'files':records,'state':'prepared'}
    (release/'manifest.json').write_text(json.dumps(manifest,indent=2))
    for item in records:
        target=Path(item['target']); target.parent.mkdir(parents=True,exist_ok=True)
        temporary=target.with_name(target.name+'.harness-new')
        temporary.write_bytes((release/f"{item['index']}.new").read_bytes())
        temporary.replace(target)
        assert sha(target)==item['after']
    manifest['state']='installed'
    (release/'manifest.json').write_text(json.dumps(manifest,indent=2))
    print(json.dumps({'phase':args.phase,'release':str(release),'files':len(records)}))


if __name__=='__main__': main()

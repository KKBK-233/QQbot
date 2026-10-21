"""同步独立搜图插件的已提交源码到公开快照，不复制运行目录、Git 元数据或未提交文件。"""
from pathlib import Path
import json
import re
import subprocess

ROOT=Path(__file__).resolve().parents[2]
SOURCE=ROOT/'MaiBot/plugins/image_reverse_search'
PUBLIC=ROOT/'data/github-publication/QQbot'
PREFIX='MaiBot/plugins/image_reverse_search/'


def git(repo,*args):
    return subprocess.check_output(['git','-C',str(repo),*args])


def main():
    """先验证全部提交内容，再按原始字节复制并记录独立仓库版本。"""
    head=git(SOURCE,'rev-parse','HEAD').decode().strip()
    blobs={}
    for entry in git(SOURCE,'ls-tree','-rz',head).split(b'\0'):
        if not entry:continue
        info,name=entry.split(b'\t',1);mode,kind,sha=info.split()
        assert mode in (b'100644',b'100755') and kind==b'blob','只同步普通源码文件'
        name=name.decode('utf-8')
        path=PUBLIC/PREFIX/name
        assert path.resolve().is_relative_to((PUBLIC/PREFIX).resolve()),'插件路径越界'
        assert not name.startswith(('.env','.git/')),'不公开凭据或 Git 目录'
        data=git(SOURCE,'cat-file','blob',sha.decode())
        assert not re.search(rb'gh[pousr]_[A-Za-z0-9]{25,}|\bsk-(?:proj-|svcacct-)?[A-Za-z0-9_-]{28,}|-----BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY',data),name
        blobs[name]=data
    for name,data in blobs.items():
        target=PUBLIC/PREFIX/name;target.parent.mkdir(parents=True,exist_ok=True);target.write_bytes(data)
    for name in git(PUBLIC,'ls-files','--',PREFIX).decode().splitlines():
        if name[len(PREFIX):] not in blobs:
            target=PUBLIC/name
            assert target.resolve().is_relative_to((PUBLIC/PREFIX).resolve())
            target.unlink()
    meta_path=PUBLIC/'SOURCE_SNAPSHOT.json';meta=json.loads(meta_path.read_text())
    meta['source_refs']['image_reverse_search']=head
    meta_path.write_text(json.dumps(meta,ensure_ascii=False,indent=2)+'\n',encoding='utf-8')
    print(json.dumps({'image_reverse_search':head,'files':len(blobs)}))


if __name__=='__main__':main()

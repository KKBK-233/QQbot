"""按真实群名添加群白名单，即时生效，不发送消息或修改人设。"""
import argparse
import copy
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import shutil
import tempfile
import tomllib
import urllib.request

import tomlkit

ROOT = Path('/home/qqbot/QQbot')


def main():
    """先核对群名与群号，再备份并原子更新名单，保留其他配置。"""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--name', required=True)
    parser.add_argument('--id', required=True, type=int)
    args = parser.parse_args()
    token = (ROOT / 'run/onebot.token').read_text().strip()
    request = urllib.request.Request('http://127.0.0.1:3000/get_group_list', data=b'{}',
        headers={'Content-Type': 'application/json', 'Authorization': 'Bearer ' + token})
    with urllib.request.urlopen(request, timeout=10) as response:
        groups = json.load(response)
    assert groups.get('status') == 'ok', '获取群列表失败'
    matches = [g for g in groups['data'] if g.get('group_name') == args.name]
    assert len(matches) == 1 and matches[0]['group_id'] == args.id, '群名或群号不匹配'
    path = ROOT / 'MaiBot/config/adapter_policy.toml'
    before = path.read_text(encoding='utf-8')
    original = tomllib.loads(before)
    doc = tomlkit.parse(before)
    group = doc['defaults']['group']
    assert group.get('default_action') == 'block', '当前默认群策略不符，请先核对'
    ids = list(group.get('allow_ids', []))
    target = str(args.id)
    if target not in ids:
        ids.append(target)
    group['allow_ids'] = ids
    after = tomlkit.dumps(doc)
    expected = copy.deepcopy(original)
    expected['defaults']['group']['allow_ids'] = ids
    assert tomllib.loads(after) == expected, '出现名单之外的配置改动'
    if after != before:
        stamp = datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')
        shutil.copy2(path, path.with_name(path.name + '.before-allow-group-' + stamp))
        with tempfile.NamedTemporaryFile(mode='w', encoding='utf-8', dir=path.parent,
                                         prefix='.adapter-policy-', delete=False) as handle:
            temporary = Path(handle.name)
            handle.write(after)
        try:
            os.chmod(temporary, path.stat().st_mode & 0o777)
            os.replace(temporary, path)
        finally:
            temporary.unlink(missing_ok=True)
    assert tomllib.loads(path.read_text(encoding='utf-8')) == expected
    print(f'已开放群聊：{args.name}（{args.id}），白名单共 {len(ids)} 个群。')


if __name__ == '__main__':
    main()

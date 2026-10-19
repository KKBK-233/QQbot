"""只读验收运行中记忆配置、历史修复进度和画像，不打印凭据或原消息。"""
import json
from pathlib import Path
import sqlite3
import urllib.request

root = Path('/home/qqbot/QQbot/MaiBot')
token = json.loads((root / 'data/webui.json').read_text())['access_token']


def read(endpoint):
    """鉴权只在本机内存中使用。"""
    req = urllib.request.Request('http://127.0.0.1:8001/api/webui/memory/' + endpoint,
                                  headers={'Cookie': 'maibot_session=' + token})
    with urllib.request.urlopen(req, timeout=40) as response:
        return json.load(response)


config = read('config')['config']
assert config['global_memory_sharing_enabled'] and config['integration']['heuristic_memory_cross_chat_enabled']
print('CONFIG', json.dumps({key: config['integration'][key] for key in (
    'heuristic_memory_recall_min_new_messages', 'heuristic_memory_recall_min_interval_seconds',
    'chat_summary_writeback_message_threshold')}, ensure_ascii=False))
state = root / 'data/a-memorix/repair-v2-state.json'
if state.exists():
    data = json.loads(state.read_text())
    print('HISTORY', json.dumps({key: data.get(key) for key in ('complete', 'people_backfill_complete', 'withdrawn')}, ensure_ascii=False))
else:
    print('HISTORY', '尚未生成进度')
with sqlite3.connect(f'{(root / "data/a-memorix/metadata/metadata.db").as_uri()}?mode=ro', uri=True) as conn:
    print('CLAIMS', conn.execute('SELECT authority,status,stability,profile_section,count(*) FROM fact_claims GROUP BY 1,2,3,4').fetchall())
    print('V2_PARAGRAPHS', conn.execute("SELECT count(*) FROM paragraphs WHERE json_extract(metadata,'$.evidence_policy')=2 AND COALESCE(is_deleted,0)=0").fetchone()[0])
for pid in ('1d69f318703030a39fa1a7e7ad6b8c7f', 'ec3f5d5db4deea753fa94499d03dc927'):
    profile = read('profiles/query?person_id=' + pid + '&limit=6')
    assert profile.get('success'), '画像接口失败'
    # 验收只输出有效段落与条目数，不公开人物原话。
    sections = {}
    title = ''
    for line in str(profile.get('profile_text', '')).splitlines():
        if line.startswith('## '):
            title = line[3:]
            sections[title] = 0
        elif line.startswith('- ') and line != '- 暂无' and title:
            sections[title] += 1
    print('PROFILE', pid, json.dumps(sections, ensure_ascii=False))

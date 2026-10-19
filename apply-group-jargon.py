"""通过本机管理接口保存两个群的黑话记忆，不发送 QQ 消息。"""

from pathlib import Path
import json
import urllib.parse
import urllib.request

ROOT = Path('/home/qqbot/QQbot')
BASE = 'http://127.0.0.1:8001/api/webui/jargon'
token = json.loads((ROOT / 'MaiBot/data/webui.json').read_text())['access_token']
# 认证只用于本机请求，不记录或输出 Cookie。
opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))


def request(path, payload=None, method=None):
    """调用官方黑话管理接口，返回结构化结果。"""
    data = None if payload is None else json.dumps(payload, ensure_ascii=False).encode()
    req = urllib.request.Request(BASE + path, data=data, method=method,
                                 headers={'Content-Type': 'application/json', 'Cookie': 'maibot_session=' + token})
    with opener.open(req, timeout=15) as response:
        result = json.load(response)
    assert result['success'], '管理接口返回失败'
    return result


def normalized_name(name):
    """忽略群名里的表情变体选择符，保留其他字符。"""
    return name.replace('\ufe0f', '')


chats = request('/chats?include_empty=true')['data']
names = {normalized_name(name) for name in ['忘记加皮肤模组了', '🕳️🕊️部']}
targets = [chat for chat in chats if chat['platform'] == 'qq' and chat['is_group']
           and normalized_name(chat['chat_name']) in names]
assert {normalized_name(chat['chat_name']) for chat in targets} == names, '目标群未找到'
session_ids = list(dict.fromkeys(chat['session_id'] for chat in targets))
entries = json.loads(Path('/mnt/d/code/web/QQbot/group-jargon.json').read_text())
# 重复运行时更新同范围的手动词条；首次保存由官方接口处理旧的 AI 解释。
for entry in entries:
    records = request('/list?search=' + urllib.parse.quote(entry['content']) + '&page_size=100')['data']
    existing = next((item for item in records if item['content'] == entry['content']
                     and item['created_by'] == 'manual' and not item['is_global']
                     and set(item['session_ids']) == set(session_ids)), None)
    payload = dict(entry, session_ids=session_ids, is_global=False)
    if existing:
        result = request('/' + str(existing['id']), dict(payload, is_jargon=True), 'PATCH')
    else:
        result = request('/', payload)
    stored = request('/' + str(result['data']['id']))['data']
    assert stored['meaning'] == entry['meaning'] and stored['is_jargon']
    assert set(stored['session_ids']) == set(session_ids) and not stored['is_global']
    print('已保存并回读验证：', stored['content'], '，生效群数：', len(stored['session_ids']))

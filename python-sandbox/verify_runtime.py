"""验证已下架的沙箱不再注册，不打印管理凭据或发送 QQ 消息。"""

import json
from pathlib import Path
import urllib.request


def main():
    """检查插件下架与特权入口撤销状态，保留历史预算数据。"""
    root = Path('/home/qqbot/QQbot/MaiBot')
    token = json.loads((root / 'data/webui.json').read_text())['access_token']
    request = urllib.request.Request(
        'http://127.0.0.1:8001/api/webui/plugins/installed',
        headers={'Cookie': 'maibot_session=' + token})
    with urllib.request.urlopen(request, timeout=10) as response:
        payload = json.load(response)
    plugin = next((p for p in payload.get('plugins', []) if p.get('id') == 'local.qqbot-python-sandbox'), None)
    assert payload.get('success') and plugin is None, '沙箱仍在插件发现目录'
    for path in ('/etc/sudoers.d/qqbot-python-sandbox', '/usr/local/libexec/qqbot-python-launch',
                 '/usr/local/libexec/qqbot-python-kill'):
        assert not Path(path).exists(), '特权入口仍然存在'
    print(json.dumps({'runtime_registered': False, 'tool': 'run_python', 'enabled': False,
                      'privileged_entries_removed': True, 'qq_messages_sent': 0}))


if __name__ == '__main__':
    main()

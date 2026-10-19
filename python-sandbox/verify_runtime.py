"""读取本机运行时注册状态，不打印管理凭据，不发送QQ消息。"""

import json
from pathlib import Path
import urllib.request


def main():
    """使用现有管理鉴权，仅检查沙箱工具是否已启用。"""
    root = Path('/home/qqbot/QQbot/MaiBot')
    token = json.loads((root / 'data/webui.json').read_text())['access_token']
    request = urllib.request.Request(
        'http://127.0.0.1:8001/api/webui/plugins/runtime/plugins/local.qqbot-python-sandbox/components',
        headers={'Cookie': 'maibot_session=' + token})
    with urllib.request.urlopen(request, timeout=10) as response:
        payload = json.load(response)
    components = payload.get('components', [])
    tool = next((c for c in components if c.get('name') == 'run_python'), None)
    assert payload.get('success') and tool and tool.get('enabled'), payload
    print(json.dumps({'runtime_registered': True, 'tool': tool['name'], 'enabled': tool['enabled'],
                      'qq_messages_sent': 0}))


if __name__ == '__main__':
    main()

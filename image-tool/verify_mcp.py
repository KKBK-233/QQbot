"""核对真实 stdio 握手和本机桥接连接，不发送任何生图请求。"""

import asyncio
import importlib.util
from pathlib import Path, PureWindowsPath
import sys
import json

folder = Path(__file__).resolve().parent
spec = importlib.util.spec_from_file_location("image_client", folder / "maibot-plugin/client.py")
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)


async def main():
    config = json.loads((folder / "config.local.json").read_text(encoding="utf-8"))
    config["data_dir"] += "-mcp-verify"
    local_config = folder.parent / "data/mcp-verify-config.json"
    local_config.parent.mkdir(exist_ok=True)
    local_config.write_text(json.dumps(config), encoding="utf-8")
    # 单独状态目录内模拟一个提交前失败的任务，只查询，不生成或覆盖真实群聊记录。
    state_root = Path(config['data_dir'])
    if sys.platform != 'win32':
        windows_root = PureWindowsPath(config['data_dir'])
        state_root = Path('/mnt', windows_root.drive[0].lower(), *windows_root.parts[1:])
    state_root.mkdir(parents=True, exist_ok=True)
    fixture = state_root / 'auth-status-fixture.json'
    fixture.write_text(json.dumps({'request_id': 'auth-status-fixture', 'state': 'failed',
        'error': '缺少 Codex ChatGPT 登录授权，请先 codex login'}), encoding='utf-8')
    original = fixture.read_bytes()
    if sys.platform == "win32":
        command = [sys.executable, str(folder / "bridge.py"), "--config", str(local_config)]
    else:
        command = ["/mnt/c/Users/668/AppData/Local/Programs/Python/Python312/python.exe",
                   "D:/code/web/QQbot/image-tool/bridge.py", "--config", "D:/code/web/QQbot/data/mcp-verify-config.json"]
    client = module.ImageClient(command)
    try:
        initialized = await client.request("initialize", {"protocolVersion": "2024-11-05", "capabilities": {},
                                                         "clientInfo": {"name": "verify", "version": "1"}})
        assert initialized["serverInfo"]["name"] == "qqbot-image-only"
        assert initialized["serverInfo"]["version"] == "1.4.0"
        listed = await client.request("tools/list", {})
        assert [t["name"] for t in listed["tools"]] == ["chatgpt_image"]
        denied = await client.request("tools/call", {"name": "eval", "arguments": {}})
        assert denied["isError"]
        checked = await client.call({'action': 'status', 'request_id': 'auth-status-fixture'})
        metadata = json.loads(checked['content'][0]['text'])
        assert metadata['state'] == 'failed'
        if config.get('backend') == 'codex':
            assert metadata['current_authorization']['server_verified'] is False
            assert '历史错误' in metadata['status_note']
        assert fixture.read_bytes() == original
        assert 'access_token' not in json.dumps(metadata)
        print("桥接1.4.0：真实 stdio 握手、单工具列表、拒绝 eval、历史失败与实时凭据传递均通过；未提交生图")
    finally:
        await client.close()


if __name__ == "__main__":
    asyncio.run(main())

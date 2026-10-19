"""通过独立 MCP 测试目录验证 Codex 生成和参考图编辑，不向 QQ 群发送测试图片。"""

import asyncio
import importlib.util
import json
from pathlib import Path
import sys
import tempfile
import time

folder = Path(__file__).resolve().parent
spec = importlib.util.spec_from_file_location("smoke_client", folder / "maibot-plugin/client.py")
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)


def metadata(result):
    first = result["content"][0]["text"]
    try:
        return json.loads(first)
    except ValueError:
        raise RuntimeError(first) from None


async def wait_for_image(client, request):
    result = metadata(await client.call(request))
    print(json.dumps({"phase": "submitted", "id": request["request_id"], "state": result["state"]}), flush=True)
    while result["state"] in ("queued", "running"):
        await asyncio.sleep(10)
        result = metadata(await client.call({"action": "status", "request_id": request["request_id"]}))
    print(json.dumps({k: v for k, v in result.items() if k != "digest"}, ensure_ascii=True), flush=True)
    if result["state"] != "succeeded":
        raise RuntimeError("生图验证未完成，请检查后端状态；不自动重试")
    return result


async def main():
    config = json.loads((folder / "config.local.json").read_text(encoding="utf-8"))
    config["backend"] = "codex"
    config["data_dir"] = str(folder.parent / "data/image-tool-codex-smoke")
    config["diagnostic_test"] = True
    with tempfile.TemporaryDirectory() as temporary:
        path = Path(temporary) / "config.json"
        path.write_text(json.dumps(config), encoding="utf-8")
        client = module.ImageClient([sys.executable, str(folder / "bridge.py"), "--config", str(path)])
        try:
            await client.request("initialize", {"protocolVersion": "2024-11-05", "capabilities": {},
                                                "clientInfo": {"name": "smoke", "version": "1"}})
            stamp = str(int(time.time()))
            generated = await wait_for_image(client, {"action": "generate", "request_id": "generate-" + stamp,
                "prompt": "生成一张简单插画：纯白背景中央只有一颗蓝色圆球，柔和阴影，不要文字。"})
            await wait_for_image(client, {"action": "edit", "request_id": "edit-" + stamp,
                "parent_id": generated["request_id"], "prompt": "编辑附图：保持构图和纯白背景，只把蓝色圆球改成红色，其他内容不变，不要文字。"})
        finally:
            await client.close()


if __name__ == "__main__":
    asyncio.run(main())

"""固定已审阅依赖并打包独立生图工具，不安装扩展或修改其他客户端配置。"""

import hashlib
import json
import os
from pathlib import Path
import zipfile


def main():
    folder = Path(__file__).resolve().parent
    root = folder.parent
    upstream = root / "downloads/imagegen-research/image-use"
    chrome = Path(os.environ["LOCALAPPDATA"]) / "Programs/chrome-use/chrome-use.exe"
    config = {
        "backend": "codex",
        "codex_transport": "native",
        "codex_image_model": "gpt-image-2.5-sunburst",
        "codex_image_quality": "high",
        "image_use": str(upstream / "image-use"),
        "image_use_sha256": hashlib.sha256((upstream / "image-use").read_bytes()).hexdigest(),
        "chrome_use": str(chrome),
        "chrome_use_sha256": hashlib.sha256(chrome.read_bytes()).hexdigest(),
        "data_dir": str(root / "data/image-tool"),
    }
    (folder / "config.local.json").write_text(json.dumps(config, indent=2), encoding="utf-8")
    destination = root / "downloads/chatgpt-image-tool-1.1.0.zip"
    portable = dict(config, image_use="vendor/image-use", data_dir="data")
    with zipfile.ZipFile(destination, "w", zipfile.ZIP_DEFLATED) as archive:
        for name in ("bridge.py", "codex_images.py", "codex_direct.py", "backend_trace.py", "image_use_runner.py", "test_bridge.py", "README.md", "audit.md"):
            archive.write(folder / name, name)
        archive.write(upstream / "image-use", "vendor/image-use")
        archive.write(upstream / "LICENSE", "vendor/LICENSE-image-use")
        for name in ("plugin.py", "client.py", "_manifest.json", "message_media.py", "qq_media.py", "character_rules.py", "ds_character.json", "character_memory.py"):
            archive.write(folder / "maibot-plugin" / name, "maibot-plugin/" + name)
        archive.writestr("config.json", json.dumps(portable, indent=2))
    print(json.dumps({"package": str(destination), "sha256": hashlib.sha256(destination.read_bytes()).hexdigest()}))


if __name__ == "__main__":
    main()

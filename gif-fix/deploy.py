"""备份后部署 GIF 动作预览与表情识别提示，保留其他运行补丁。"""

import argparse
from datetime import datetime, timezone
from pathlib import Path

SOURCE = Path(__file__).resolve().parent


def contents(root):
    """仅替换抽帧函数，避免覆盖上游文件中的其他修改。"""
    relative = "src/common/utils/utils_image.py"
    text = (root / relative).read_text(encoding="utf-8")
    start = text.index("    @staticmethod\n    def gif_2_static_image(")
    end = text.index("    @staticmethod\n    def compress_image_to_size(", start)
    text = text[:start] + '''    @staticmethod
    def gif_2_static_image(gif_bytes: bytes, similarity_threshold: float = 1000.0, max_frames: int = 15) -> bytes:
        """按播放时间覆盖整段 GIF，以编号网格展示眨眼、歪头等动作。"""
        from src.common.utils.gif_preview import gif_preview

        return gif_preview(gif_bytes, similarity_threshold, max_frames)

''' + text[end:]
    return {
        relative: text,
        "src/common/utils/gif_preview.py": (SOURCE / "preview.py").read_text(encoding="utf-8"),
        "prompts/zh-CN/emoji_content_analysis.prompt": (SOURCE / "emoji.prompt").read_text(encoding="utf-8"),
    }


def deploy(root):
    """先校验全部源码，再备份并幂等写入。"""
    prepared = contents(root)
    for relative, text in prepared.items():
        if relative.endswith(".py"):
            compile(text, relative, "exec")
    backup = root / "data/gif-fix-backups" / datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
    for relative, text in prepared.items():
        path = root / relative
        if path.exists() and path.read_text(encoding="utf-8") == text:
            continue
        if path.exists():
            old = backup / relative
            old.parent.mkdir(parents=True, exist_ok=True)
            # 备份内容即可；Windows 挂载盘不一定允许 Linux 修改文件时间。
            old.write_bytes(path.read_bytes())
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")
        print("已部署：" + relative)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=Path("/home/qqbot/QQbot/MaiBot"))
    deploy(parser.parse_args().root)

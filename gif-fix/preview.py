"""将 GIF 按时间采样为有序网格，让视觉模型看到整段动作。"""

from bisect import bisect_right
from math import ceil
import io

from PIL import Image, ImageDraw


def gif_preview(gif_bytes: bytes, similarity_threshold: float = 1000.0, max_frames: int = 15) -> bytes:
    """保留旧调用参数，改用时间采样，避免像素溢出与细微动作被阈值丢弃。"""
    if max_frames < 1:
        raise ValueError("最大抽帧数量必须大于零")
    # similarity_threshold 仅保留调用兼容；眨眼、歪头不能按全图差异阈值过滤。
    with Image.open(io.BytesIO(gif_bytes)) as animation:
        if animation.format != "GIF":
            raise ValueError("输入的图片不是有效的 GIF 格式")
        frame_count = animation.n_frames
        starts = []
        elapsed = 0
        for index in range(frame_count):
            animation.seek(index)
            starts.append(elapsed)
            duration = max(10, int(animation.info.get("duration", 100) or 100))
            elapsed += duration

        # 覆盖起止及中间时刻，按播放时长而非文件帧数取样。
        count = min(frame_count, max_frames, 12)
        if count == 1:
            indexes = [0]
        else:
            end = elapsed - 1
            indexes = sorted({bisect_right(starts, end * slot / (count - 1)) - 1 for slot in range(count)})
        columns = min(4, len(indexes))
        cell_side, caption_height = 256, 24
        rows = ceil(len(indexes) / columns)
        sheet = Image.new("RGB", (columns * cell_side, rows * (cell_side + caption_height)), "white")
        draw = ImageDraw.Draw(sheet)
        for slot, index in enumerate(indexes):
            animation.seek(index)
            # Pillow 合成 GIF 的局部更新与 disposal；透明区域铺白底。
            rgba = animation.convert("RGBA")
            frame = Image.new("RGB", rgba.size, "white")
            frame.paste(rgba, mask=rgba.getchannel("A"))
            frame.thumbnail((cell_side, cell_side), Image.Resampling.LANCZOS)
            x = (slot % columns) * cell_side
            y = (slot // columns) * (cell_side + caption_height)
            draw.text((x + 6, y + 5), f"{slot + 1}: frame {index + 1}/{frame_count}  {starts[index] / 1000:.2f}s", fill="black")
            sheet.paste(frame, (x + (cell_side - frame.width) // 2, y + caption_height + (cell_side - frame.height) // 2))
        output = io.BytesIO()
        sheet.save(output, format="JPEG", quality=92)
        return output.getvalue()

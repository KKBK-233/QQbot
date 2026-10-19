"""验证完整动作、时长采样、透明背景及异常输入，不调用模型。"""

import io
import unittest

from PIL import Image

from preview import gif_preview


def animation(colors, durations=100):
    """构造带明确时间与帧差的独立 GIF 样本。"""
    frames = [Image.new("RGB", (64, 64), color) for color in colors]
    output = io.BytesIO()
    frames[0].save(output, format="GIF", save_all=True, append_images=frames[1:], duration=durations, loop=0)
    return output.getvalue()


def cell_color(sheet, column, row=0):
    return sheet.getpixel((column * 256 + 128, row * 280 + 24 + 128))


class PreviewTests(unittest.TestCase):
    def test_large_changes_survive_uint8_overflow(self):
        """旧 uint8 平方无法达到 1000 阈值；新预览必须保留三个动作阶段。"""
        sheet = Image.open(io.BytesIO(gif_preview(animation(["black", "white", "red"]), max_frames=3)))
        self.assertEqual(sheet.size, (768, 280))
        self.assertLess(max(cell_color(sheet, 0)), 5)
        self.assertGreater(min(cell_color(sheet, 1)), 245)
        self.assertGreater(cell_color(sheet, 2)[0], 245)

    def test_time_sampling_keeps_long_middle_pose(self):
        sheet = Image.open(io.BytesIO(gif_preview(animation(["red", "green", "blue", "white"], [10, 1000, 10, 10]), max_frames=3)))
        self.assertGreater(cell_color(sheet, 0)[0], 240)
        self.assertGreater(cell_color(sheet, 1)[1], 100)
        self.assertGreater(min(cell_color(sheet, 2)), 240)

    def test_grid_bounds_and_end_frame(self):
        colors = [(i * 10, 0, 0) for i in range(20)]
        sheet = Image.open(io.BytesIO(gif_preview(animation(colors))))
        self.assertEqual(sheet.size, (1024, 840))
        self.assertAlmostEqual(cell_color(sheet, 3, 2)[0], 190, delta=5)

    def test_single_frame_and_single_frame_limit(self):
        for data in [animation(["red"]), animation(["red", "blue"])]:
            sheet = Image.open(io.BytesIO(gif_preview(data, max_frames=1)))
            self.assertEqual(sheet.size, (256, 280))

    def test_transparency_composited_on_white(self):
        frame = Image.new("RGBA", (64, 64), (0, 0, 0, 0))
        output = io.BytesIO()
        frame.save(output, format="GIF", transparency=0)
        sheet = Image.open(io.BytesIO(gif_preview(output.getvalue())))
        self.assertGreater(min(cell_color(sheet, 0)), 245)

    def test_invalid_input_and_limit(self):
        with self.assertRaises(ValueError):
            gif_preview(animation(["red"]), max_frames=0)
        output = io.BytesIO()
        Image.new("RGB", (64, 64)).save(output, format="PNG")
        with self.assertRaises(ValueError):
            gif_preview(output.getvalue())


if __name__ == "__main__":
    unittest.main()

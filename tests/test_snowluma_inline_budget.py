"""SnowLuma 适配器入站媒体预算：大合并转发不再撑破 16 MiB IPC 单帧而被整条丢弃。"""
from pathlib import Path
from types import ModuleType, SimpleNamespace
import asyncio
import base64
import importlib
import json
import sys
import unittest

ADAPTER = Path(__file__).resolve().parents[1] / 'MaiBot' / 'plugins' / 'MaiBot-SnowLuma-Adapter'
PACKAGE = 'snowluma_adapter_under_test'
MAX_FRAME_SIZE = 16 * 1024 * 1024

# 公开源码快照只保存适配器补丁，未安装独立插件时明确跳过本组测试。
if not (ADAPTER / 'codecs' / 'inbound' / 'message_codec.py').is_file():
    raise unittest.SkipTest('未安装独立 SnowLuma 适配器，跳过入站媒体预算测试')

package = ModuleType(PACKAGE)
package.__path__ = [str(ADAPTER)]
sys.modules.setdefault(PACKAGE, package)
codec_module = importlib.import_module(f'{PACKAGE}.codecs.inbound.message_codec')


class FakeLogger:
    def __init__(self):
        self.warnings = []

    def warning(self, message, *args, **kwargs):
        self.warnings.append(message)

    def __getattr__(self, name):
        return lambda *args, **kwargs: None


class FakeQueryService:
    async def download_binary(self, url):
        return None

    async def safe_call_action_data(self, action, params):
        return None

    async def get_forward_message(self, message_id):
        return None


def image(index: int, size: int):
    """每张图内容不同，避免 hash 相同。"""
    raw = bytes([index % 251]) * size
    return {'type': 'image', 'data': {'base64': base64.b64encode(raw).decode(), 'sub_type': 0}}


def build(segments, message_id='m1'):
    logger = FakeLogger()
    profile_state = SimpleNamespace(profile=SimpleNamespace(client_type='snowluma', record_out_format='wav'))
    codec = codec_module.QQInboundCodec(logger, FakeQueryService(), profile_state, lambda: SimpleNamespace())
    payload = {'message_type': 'private', 'message_id': message_id, 'time': 1, 'message': segments}
    result = asyncio.run(codec.build_message_dict(payload, '10000', '20000', {'nickname': 'admin'}))
    return result, logger


def forward_images(nodes):
    return [segment for node in nodes for segment in node['content'] if segment['type'] in ('image', 'emoji')]


class InlineBudgetTest(unittest.TestCase):
    def test_large_forward_keeps_order_and_fits_frame(self):
        # 复现线上 52 MB 帧：一条转发里 40 张 1 MiB 图片
        nodes = [{'type': 'node', 'data': {'user_id': '1', 'nickname': 'a', 'content': [image(i, 1024 * 1024)]}}
                 for i in range(40)]
        result, logger = build([{'type': 'text', 'data': {'text': '帮我看看'}},
                                {'type': 'forward', 'data': {'id': 'f1', 'content': nodes}}])
        encoded = len(json.dumps(result, ensure_ascii=False).encode('utf-8'))
        self.assertLess(encoded, MAX_FRAME_SIZE)
        forward = next(s for s in result['raw_message'] if s['type'] == 'forward')
        images = forward_images(forward['data'])
        self.assertEqual(len(images), 40)  # 序号不丢
        inlined = [s for s in images if 'binary_data_base64' in s]
        self.assertEqual(len(inlined), 8)
        self.assertTrue(all('binary_data_base64' in s for s in images[:8]))
        self.assertTrue(all(s['hash'] and s['data'] == codec_module.OVER_BUDGET_DESCRIPTION for s in images[8:]))
        self.assertTrue(any('32 个媒体未内嵌原图' in w for w in logger.warnings))

    def test_small_message_unchanged(self):
        result, logger = build([image(1, 1024), image(2, 2048)])
        self.assertTrue(all('binary_data_base64' in s for s in result['raw_message']))
        self.assertEqual(logger.warnings, [])

    def test_budget_is_per_message(self):
        for message_id in ('a', 'b'):
            result, _ = build([image(i, 1024 * 1024) for i in range(6)], message_id)
            self.assertTrue(all('binary_data_base64' in s for s in result['raw_message']))

    def test_oversized_text_strips_media(self):
        result, logger = build([{'type': 'text', 'data': {'text': '字' * (5 * 1024 * 1024)}}, image(1, 1024)])
        picture = next(s for s in result['raw_message'] if s['type'] == 'image')
        self.assertNotIn('binary_data_base64', picture)
        self.assertTrue(any('已去掉 1 个内嵌媒体' in w for w in logger.warnings))


if __name__ == '__main__':
    unittest.main()

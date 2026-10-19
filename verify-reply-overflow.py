"""用真实被替换的回答离线回放后处理，确认修复而不发送 QQ 消息。"""

from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from unittest.mock import patch

import io
import json
import os
import sys

root = Path('/home/qqbot/QQbot/MaiBot')
os.chdir(root)
sys.path.insert(0, str(root))


def text_fields(value):
    """仅提取模型输出文本，不读取请求或凭据字段。"""
    if isinstance(value, dict):
        for key, item in value.items():
            if key == 'text' and isinstance(item, str):
                yield item
            elif isinstance(item, (dict, list)):
                yield from text_fields(item)
    elif isinstance(value, list):
        for item in value:
            yield from text_fields(item)


paths = sorted((root / 'logs/maisaka_prompt/replyer/qq_group_1022382380').glob('*.json'),
               key=lambda path: path.stat().st_mtime, reverse=True)
original = None
for path in paths:
    outputs = json.loads(path.read_text()).get('output_items', [])
    original = next((text for text in text_fields(outputs) if '你这思路对，分步别一步到位' in text), None)
    if original:
        break
assert original, '未找到本次被替换回答的模型输出快照'
with redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()):
    from src.config.config import global_config
    from src.chat.utils.utils import ProcessedResponseSegment, process_llm_response_segments
    # 错别字纠正会随机增加分句，固定回放日志中的10条后处理结果。
    boundaries = [len(original) * index // 10 for index in range(11)]
    segments = [ProcessedResponseSegment(original[boundaries[index]:boundaries[index + 1]]) for index in range(10)]
    with patch('src.chat.utils.utils._apply_response_post_processing', return_value=segments):
        global_config.response_splitter.enable_overflow_return_all = False
        old = process_llm_response_segments(original, enable_chinese_typo=False)
        global_config.response_splitter.enable_overflow_return_all = True
        new = process_llm_response_segments(original, enable_chinese_typo=False)
    assert 'MobileFaceNet' not in '\n'.join(segment.text for segment in old)
    assert 'MobileFaceNet' in '\n'.join(segment.text for segment in new)
    assert len(new) == 1
print('真实回答按日志10条分句回放通过：旧配置丢弃答案，新配置保留答案且仅发1条。')

"""Haiku缓存布局与数字遥测；保留所有内容、消息顺序和系统权限。"""
from copy import deepcopy
import json
import os
from pathlib import Path
import re

STATUS_MARKER = '\n【本会话生图任务只读状态】'
CLOCK = re.compile(r'^【宿主本轮上下文】\s*(?:当前时间|时间)：\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}\s*$')
PRIORITY_PREFIX = '宿主已通过真实账号的@标记或配置的唤醒名确认：'
RULE_PREFIX = '你的身份是成年时期的《魔女之旅》伊蕾娜，'


def with_prompt_cache(payload):
    """只缓存工具和固定system前缀，不缓存会滚动或每轮变化的聊天历史。"""
    result = deepcopy(payload)
    tools = result.get('tools') or []
    # 工具名及参数键稳定；保留数组顺序、工具描述和所有Schema语义。
    if tools:
        canonical = [json.loads(json.dumps(tool, ensure_ascii=False, sort_keys=True)) for tool in tools]
        canonical.sort(key=lambda tool: tool['name'])
        canonical[-1]['cache_control'] = {'type': 'ephemeral'}
        result['tools'] = canonical
    fixed, dynamic = [], []
    for block in result.get('system') or []:
        text = block.get('text', '')
        # 只移动明确已知的时间与本轮@提醒；未知系统规则不猜测其用途。
        if block.get('type') == 'text' and (CLOCK.fullmatch(text) or text.startswith(PRIORITY_PREFIX)):
            dynamic.append(block)
        elif block.get('type') == 'text' and text.startswith(RULE_PREFIX) and STATUS_MARKER in text:
            instruction, separator, status = text.partition(STATUS_MARKER)
            fixed.append({**block, 'text': instruction})
            dynamic.append({**block, 'text': separator + status})
        else:
            fixed.append(block)
    if fixed:
        # 固定系统前缀不足服务端最小长度时会自然跳过，不填充无意义内容。
        fixed[-1]['cache_control'] = {'type': 'ephemeral'}
    if fixed or dynamic:
        result['system'] = fixed + dynamic
    return result


def numeric_usage(data):
    """缓存计数缺失返回null，不把未上报误记为0。"""
    usage = data.get('usage') if isinstance(data, dict) else None
    usage = usage if isinstance(usage, dict) else {}
    keys = ('input_tokens', 'cache_creation_input_tokens', 'cache_read_input_tokens', 'output_tokens')
    return {key: usage.get(key) if type(usage.get(key)) is int and usage[key] >= 0 else None for key in keys}


def record_cache_usage(path, data, task_name, session_hash):
    """私有日志只记录固定任务枚举、会话哈希和数字；失败不改变模型结果。"""
    import time
    counts = numeric_usage(data)
    row = {'logged_at': time.time(), 'task': task_name if task_name in {'planner', 'replyer'} else 'other',
           'session_hash': session_hash if re.fullmatch(r'[0-9a-f]{32}', session_hash or '') else None, **counts}
    inputs = [counts[key] for key in ('input_tokens', 'cache_creation_input_tokens', 'cache_read_input_tokens')]
    total = sum(inputs) if all(value is not None for value in inputs) else None
    row['total_input_tokens'] = total
    row['token_hit_rate_percent'] = round(counts['cache_read_input_tokens'] / total * 100, 2) if total else None
    path = Path(path)
    try:
        path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
        try:
            os.fchmod(fd, 0o600)
            encoded = (json.dumps(row, ensure_ascii=False) + '\n').encode()
            # 一条小日志一次追加，避免并发请求拆分数字记录。
            if os.write(fd, encoded) != len(encoded):
                return False
        finally:
            os.close(fd)
        return True
    except OSError:
        return False

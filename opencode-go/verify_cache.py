"""隔离测量Haiku套餐缓存：当前无标记请求与显式缓存对照，不改变生产。"""
import argparse
import hashlib
import json
import os
from pathlib import Path
import secrets
import time
import tomllib

import httpx

from protocol import BASE, MODEL, messages_payload, response_metadata, response_parts

ROOT = Path('/home/qqbot/QQbot/MaiBot')
KEY_FILE = Path.home() / '.config/maibot-opencode-go/api-key'


def source_fixture(run_id, phase):
    """构造固定的合成Python源码前缀，避免发送真实项目或群聊数据。"""
    functions = []
    for index in range(96):
        functions.append(f'''def first_{index:03d}(items, default=None):
    # 编码审查样本：检查空序列、合法类型和默认返回值。
    if items is None:
        return default
    if not isinstance(items, (list, tuple)):
        raise TypeError("items must be list or tuple")
    return items[0] if items else default
''')
    return (f'隔离编码缓存测试编号：{run_id}，对照组：{phase}。\n'
            '你是Python代码审查助手。以下是合成仓库源码，不运行代码或调用工具。'
            '收到审查请求后只回复 CACHE_PROBE_OK，不给解释。\n\n' + '\n'.join(functions))


def probe_payload(run_id, phase, round_index):
    """每组固定源码、逐轮改变短问题；只有对照组显式添加缓存断点。"""
    system = source_fixture(run_id, phase)
    messages = [{'role': 'system', 'content': system},
                {'role': 'user', 'content': f'请检查first_{round_index:03d}是否安全处理空列表；仅按规则回复。'}]
    payload = messages_payload(messages, max_tokens=64)
    if phase == 'explicit':
        payload['system'][-1]['cache_control'] = {'type': 'ephemeral'}
    return payload


def summarize(rows):
    """分母包括新输入、缓存写入和缓存读取，冷启动单独显示。"""
    total = sum(row['total_input_tokens'] for row in rows)
    cached = sum(row['cache_read_input_tokens'] for row in rows)
    hits = sum(row['cache_read_input_tokens'] > 0 for row in rows)
    warm = rows[1:]
    warm_total = sum(row['total_input_tokens'] for row in warm)
    return {'requests': len(rows), 'hit_requests': hits,
            'request_hit_rate_percent': round(hits / len(rows) * 100, 2) if rows else None,
            'input_tokens': total, 'cached_tokens': cached,
            'token_hit_rate_percent': round(cached / total * 100, 2) if total else None,
            'warm_request_hit_rate_percent': round(sum(row['cache_read_input_tokens'] > 0 for row in warm) / len(warm) * 100, 2) if warm else None,
            'warm_token_hit_rate_percent': round(sum(row['cache_read_input_tokens'] for row in warm) / warm_total * 100, 2) if warm_total else None}


def private_save(path, data):
    """报告只保存服务端数字和诊断元数据，不保存源码、响应或密钥。"""
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    temporary = path.with_name(path.name + '.tmp')
    fd = os.open(temporary, os.O_CREAT | os.O_WRONLY | os.O_TRUNC, 0o600)
    with os.fdopen(fd, 'w') as handle:
        json.dump(data, handle, ensure_ascii=False, indent=2)
    temporary.chmod(0o600)
    temporary.replace(path)


def main(rounds=3):
    """顺序执行最多六次编码请求；失败停止，不重试、不切按量付费。"""
    if not 2 <= rounds <= 3:
        raise ValueError('每组仅允许2至3轮')
    if KEY_FILE.stat().st_mode & 0o077:
        raise ValueError('密钥权限必须为0600')
    key = KEY_FILE.read_text().strip()
    if not key.startswith('oc_sk_'):
        raise ValueError('凭据格式无效')
    config = ROOT / 'config/model_config.toml'
    before_tasks = tomllib.loads(config.read_text())['model_task_config']
    run_id = secrets.token_hex(8)
    session = 'qqbot-' + hashlib.sha256(('haiku-cache-' + run_id).encode()).hexdigest()[:32]
    headers = {'x-api-key': key, 'anthropic-version': '2023-06-01',
               'User-Agent': 'QQbot-OpenCode-Integration/1.0', 'x-opencode-session': session}
    report = {'model': MODEL, 'run_id': run_id, 'success': False, 'groups': {}, 'tools_executed': 0,
              'qq_messages_sent': 0, 'memory_writes': 0, 'production_tasks_changed': False}
    path = ROOT / ('data/opencode-go/cache-probe-' + run_id + '.json')
    from protocol import PROXY
    with httpx.Client(proxy=PROXY, trust_env=False, follow_redirects=False, timeout=httpx.Timeout(90, connect=10)) as client:
        for phase in ['baseline', 'explicit']:
            rows = []
            report['groups'][phase] = {'rows': rows}
            for index in range(rounds):
                payload = probe_payload(run_id, phase, index)
                started = time.monotonic()
                try:
                    response = client.post(BASE + '/messages', headers=headers, json=payload)
                    if response.status_code != 200:
                        row = {'round': index + 1, 'http_status': response.status_code, 'success': False}
                        rows.append(row)
                        private_save(path, report)
                        print(json.dumps({'phase': phase, **row}, ensure_ascii=False), flush=True)
                        return 1
                    data = response.json()
                    _, calls, counts = response_parts(data)
                    if calls:
                        raise ValueError('缓存探针不应调用工具')
                    usage = response_metadata(data)['usage']
                    if not all(k in usage for k in ['input_tokens', 'cache_creation_input_tokens', 'cache_read_input_tokens']):
                        raise ValueError('缺少缓存计数，不能把缺失字段当作零')
                    row = {'round': index + 1, 'success': True, 'http_status': response.status_code,
                           'elapsed_seconds': round(time.monotonic() - started, 2), **usage,
                           'total_input_tokens': counts['prompt'],
                           'token_hit_rate_percent': round(counts['cache_hit'] / counts['prompt'] * 100, 2) if counts['prompt'] else None}
                except (httpx.RequestError, ValueError, KeyError, TypeError) as error:
                    row = {'round': index + 1, 'success': False, 'error_type': type(error).__name__}
                    rows.append(row)
                    private_save(path, report)
                    print(json.dumps({'phase': phase, **row}, ensure_ascii=False), flush=True)
                    return 1
                rows.append(row)
                private_save(path, report)
                print(json.dumps({'phase': phase, **row}, ensure_ascii=False), flush=True)
            report['groups'][phase]['summary'] = summarize(rows)
    report['production_tasks_changed'] = tomllib.loads(config.read_text())['model_task_config'] != before_tasks
    report['success'] = not report['production_tasks_changed']
    private_save(path, report)
    print(json.dumps({'success': report['success'], 'summaries': {key: value['summary'] for key, value in report['groups'].items()},
                      'report_name': path.name, 'production_tasks_changed': report['production_tasks_changed']}, ensure_ascii=False), flush=True)
    return 0 if report['success'] else 1


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('--rounds', type=int, default=3)
    args = parser.parse_args()
    raise SystemExit(main(args.rounds))

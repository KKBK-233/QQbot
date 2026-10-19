"""隔离对比 Haiku 思考设置；只记录可见回答与工具，不执行或发送。"""
import asyncio
from contextlib import redirect_stderr, redirect_stdout
from copy import deepcopy
import hashlib
import io
import json
import os
from pathlib import Path
import sys
import time

import httpx

ROOT = Path('/home/qqbot/QQbot/MaiBot')


def synthetic_payload():
    """复用真实规划器规则和工具定义，移除群聊正文、画像及历史工具结果。"""
    os.chdir(ROOT)
    sys.path.insert(0, str(ROOT))
    from src.plugin_runtime.hook_payloads import deserialize_prompt_items
    from src.llm_models.payload_content.context_item import CONTEXT_ITEM_SCHEMA_VERSION
    from src.llm_models.payload_content.context_protocol import ContextProtocolMode
    from src.llm_models.model_client.openai_client import _convert_messages
    from protocol import messages_payload
    from cache_layout import with_prompt_cache, STATUS_MARKER, RULE_PREFIX

    folder = ROOT / 'logs/maisaka_prompt/planner/qq_group_1022382380'
    snapshot = max(folder.glob('*.json'), key=lambda p: p.stat().st_mtime)
    doc = json.loads(snapshot.read_text())
    items = deserialize_prompt_items(doc['request_items'], item_schema_version=CONTEXT_ITEM_SCHEMA_VERSION,
                                     mode=ContextProtocolMode.REQUEST_CONTEXT)
    messages = []
    for msg in _convert_messages(items):
        if msg['role'] not in {'system', 'developer'}:
            continue
        msg = deepcopy(msg)
        content = msg.get('content')
        if isinstance(content, str) and content.startswith(RULE_PREFIX) and STATUS_MARKER in content:
            msg['content'] = content.partition(STATUS_MARKER)[0]
        elif isinstance(content, list):
            for part in content:
                text = part.get('text', '')
                if text.startswith(RULE_PREFIX) and STATUS_MARKER in text:
                    part['text'] = text.partition(STATUS_MARKER)[0]
        messages.append(msg)
    messages.append({'role': 'user', 'content': '隔离合成任务，稍后指定。'})
    tools = []
    for definition in doc['tool_definitions']:
        function = definition.get('function', definition)
        tools.append({'name': function['name'], 'description': function.get('description', ''),
                      'parameters': function.get('parameters', function.get('parameters_schema'))})
    return with_prompt_cache(messages_payload(messages, tools, 2048))


async def diagnose():
    """每个案例各请求一次关闭思考和 adaptive/medium，不改变生产设置。"""
    payload = synthetic_payload()
    key_path = Path.home() / '.config/maibot-opencode-go/api-key'
    if key_path.stat().st_mode & 0o077:
        raise RuntimeError('凭据权限不正确')
    from protocol import BASE, MODEL, PROXY
    scenarios = [
        ('choose-one', 'msg-choice', '我不是问排名，只问你的个人偏好：在 DeepSeek、Gemini、ChatGPT 中选一个最喜欢的，不要反问，不要都选。'),
        ('temporary-tone', 'msg-tone', '保持你的身份和性格不变，就用稍微吃醋的语气说一句欢迎回来，不要动作旁白。'),
        ('retry-image', 'msg-retry', '上一张海边日常服图片因为授权缺失失败了，现在管理员已恢复授权。请重试生成，使用新的任务编号。已有失败任务编号 old-beach；不要仅查询旧任务。'),
    ]
    results = []
    async with httpx.AsyncClient(proxy=PROXY, trust_env=False, follow_redirects=False, timeout=75) as http:
        for label, message_id, request in scenarios:
            for mode in ['disabled', 'adaptive-medium']:
                body = deepcopy(payload)
                task = {'task_message_id': message_id, 'addressed_to_self': True, 'request': request, 'image_analysis': None}
                body['messages'] = [{'role': 'user', 'content': [{'type': 'text', 'text':
                    '【待处理的用户任务与参考数据】\n' + json.dumps(task, ensure_ascii=False)}]}]
                if mode == 'adaptive-medium':
                    body['thinking'] = {'type': 'adaptive'}
                    body['output_config'] = {'effort': 'medium'}
                headers = {'x-api-key': key_path.read_text().strip(), 'anthropic-version': '2023-06-01',
                           'User-Agent': 'QQbot-OpenCode-Integration/1.0',
                           'x-opencode-session': 'qqbot-' + hashlib.sha256(('behavior-' + mode).encode()).hexdigest()[:32]}
                started = time.monotonic()
                response = await http.post(BASE + '/messages', headers=headers, json=body)
                row = {'scenario': label, 'mode': mode, 'http_status': response.status_code,
                       'seconds': round(time.monotonic() - started, 2)}
                if response.status_code == 200:
                    data = response.json()
                    if data.get('model') != MODEL:
                        raise RuntimeError('服务端返回型号不一致')
                    # 原生思考正文和签名既不输出，也不保存；仅统计块数。
                    row.update(model=data['model'], stop_reason=data.get('stop_reason'), usage=data.get('usage'),
                               thinking_blocks=sum(p.get('type') in {'thinking', 'redacted_thinking'} for p in data['content']),
                               text=[p['text'] for p in data['content'] if p.get('type') == 'text'],
                               tools=[{'name': p['name'], 'input': p['input']} for p in data['content'] if p.get('type') == 'tool_use'])
                results.append(row)
    report = {'results': results, 'production_config_changed': False, 'tools_executed': 0,
              'qq_messages_sent': 0, 'private_chat_history_sent': False}
    out = Path('/mnt/d/code/web/QQbot/data/haiku-diagnosis/behavior-comparison.json')
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding='utf-8')
    return report


if __name__ == '__main__':
    try:
        with redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()):
            result = asyncio.run(diagnose())
    except Exception as error:
        print(json.dumps({'success': False, 'error_type': type(error).__name__}))
        raise SystemExit(1)
    print(json.dumps(result, ensure_ascii=False))

"""用MaiBot真实回复客户端做一次网络验证，可明确选择向原群发送测试结果。"""
from contextlib import redirect_stdout, redirect_stderr
from pathlib import Path
import argparse
import asyncio
import io
import json
import os
import sys
import time
import urllib.request

ROOT = Path('/home/qqbot/QQbot/MaiBot')


async def verify(send):
    """只调用回复模型，不执行模型工具；群内测试消息有明确标记。"""
    os.chdir(ROOT)
    sys.path.insert(0, str(ROOT))
    from src.services import llm_service
    persona = json.loads(Path('/mnt/d/code/web/QQbot/persona-elaina.json').read_text())
    started = time.monotonic()
    generated = await llm_service.generate(llm_service.LLMServiceRequest(
        task_name='replyer', request_type='manual.network_verification', max_tokens=80,
        prompt=[{'role': 'system', 'content': persona['personality']},
                {'role': 'user', 'content': '这是管理员授权的网络测试。请简短回复：网络测试通过，伊蕾娜在线。'}]))
    assert generated.success, '真实回复客户端未成功'
    text = generated.completion.response.strip()
    assert text and len(text) < 250
    result = {'model_success': True, 'model_seconds': round(time.monotonic()-started, 3), 'reply': text}
    if send:
        token = (ROOT.parent / 'run/onebot.token').read_text().strip()
        payload = {'group_id': 894179464, 'message': [{'type': 'text', 'data': {'text': '[网络测试] ' + text}}]}
        request = urllib.request.Request('http://127.0.0.1:3000/send_group_msg', data=json.dumps(payload).encode(),
            headers={'Content-Type': 'application/json', 'Authorization': 'Bearer ' + token})
        with urllib.request.urlopen(request, timeout=15) as response:
            sent = json.load(response)
        result.update(qq_status=sent.get('status'), qq_retcode=sent.get('retcode'), qq_message_id=(sent.get('data') or {}).get('message_id'))
        assert result['qq_status'] == 'ok' and result['qq_message_id'], '群发送没有明确成功回执'
    return result


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--send', action='store_true', help='向原群发送一次带网络测试标记的模型回复')
    args = parser.parse_args()
    capture = io.StringIO()
    try:
        with redirect_stdout(capture), redirect_stderr(capture):
            result = asyncio.run(verify(args.send))
    except Exception as error:
        print(json.dumps({'success': False, 'error_type': type(error).__name__}))
        raise SystemExit(1)
    print(json.dumps(result, ensure_ascii=False))

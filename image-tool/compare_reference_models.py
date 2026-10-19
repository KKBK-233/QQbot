"""固定原图、文字和工具做三组交替对照，不执行模型返回的工具。"""
import asyncio
from contextlib import redirect_stdout, redirect_stderr
import io
import json
from pathlib import Path

from verify_group_reference import verify

async def main():
    """交替请求减少顺序影响，保留每次选择；结果仅解释此案例。"""
    rows = []
    for round_id in range(1, 4):
        for model in ['deepseek-v4-flash-thinking', 'opencode-go-haiku-5.5-medium']:
            try:
                row = await verify(False, model=model, observe=True)
            except Exception as exc:
                row = {'success': False, 'error_type': type(exc).__name__}
            rows.append({'round': round_id, 'requested_model': model, **row})
    hashes = {row['input_hash'] for row in rows if row.get('input_hash')}
    report = {'same_input': len(hashes) == 1 and all(row.get('input_hash') for row in rows),
              'results': rows, 'generation_requests': 0, 'qq_messages_sent': 0}
    path = Path('/mnt/d/code/web/QQbot/data/opencode-go/original-reference-comparison.json')
    path.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding='utf-8')
    return [{k: row.get(k) for k in ['round', 'requested_model', 'model', 'seconds', 'original_selected',
                                   'selected_action', 'selected_message_id', 'error_type']} for row in rows]

if __name__ == '__main__':
    with redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()):
        report = asyncio.run(main())
    print(json.dumps(report, ensure_ascii=False))

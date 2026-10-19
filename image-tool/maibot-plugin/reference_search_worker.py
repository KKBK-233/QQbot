"""只读检索沙箱内搜索公开图片，输出原图链接和来源，不读取账号目录。"""
import json
import sys

sys.path.insert(0, '/deps')
from ddgs import DDGS

if __name__ == '__main__':
    try:
        payload = json.load(sys.stdin)
        if payload['backend'] not in ('duckduckgo', 'bing'):
            raise ValueError('不支持的图片检索引擎')
        rows = DDGS(proxy=payload['proxy'] or None, timeout=4).images(
            payload['query'], backend=payload['backend'], max_results=5, region='us-en')
        # 只传少量有界字段，避免上游额外数据扩张IPC输出或混入其他指令。
        bounded = []
        for row in rows[:5]:
            if not all(isinstance(row.get(key), str) and len(row[key]) <= 2048 for key in ('image', 'url')):
                continue
            bounded.append({'title': str(row.get('title') or '')[:160], 'image': row['image'], 'url': row['url']})
        print(json.dumps({'success': True, 'results': bounded}, ensure_ascii=False))
    except Exception:
        print(json.dumps({'success': False}))

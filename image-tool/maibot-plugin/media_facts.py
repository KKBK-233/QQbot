"""把宿主实际可读取的群图片状态传给模型，避免把视觉摘要误当纯文字。"""
import asyncio
import json
import re
import time

from .message_media import read_images, MediaUnavailable


async def available_media(ctx, kwargs, budget=1.5):
    """限时检查四个目标，优先待处理任务及明确绘图引用，附带宿主摘要。"""
    session = str(kwargs.get('session_id') or '')
    if not session:
        return []
    messages, pending, current = [], [], None
    for item in kwargs.get('items', []):
        if item.get('item_type') != 'UserMessageItem':
            continue
        text = ''.join(p.get('text', '') for p in item.get('parts', []) if p.get('type') == 'text')
        header = text.split('\n', 1)[0]
        match = re.match(r'<message\b[^>]*\bmsg_id="([^"]+)"', header)
        if text.startswith('【待处理的用户任务与参考数据】\n'):
            try:
                task = json.loads(text.split('\n', 1)[1])
                if isinstance(task, dict) and task.get('task_message_id'):
                    pending.append(str(task['task_message_id']))
            except (ValueError, TypeError):
                pass
            continue
        if match:
            current = None
            if 'is_self_message="true"' not in header:
                current = {'id': match.group(1), 'header': header, 'text': text.split('\n', 1)[-1]}
                messages.append(current)
            continue
        # 宿主会把一条消息的标题、@和正文拆成多个公开 Context Item。
        if current is not None and not text.startswith('【'):
            current['text'] += '\n' + text
    drawing = re.compile(r'生图|画|生成|修改|改图|这张|那张')
    referenced = [m['id'] for m in reversed(messages)
                  if re.search(r'\bquote="[^"]+"', m['header']) and drawing.search(m['text'])][:2]
    selected = list(dict.fromkeys([*reversed(pending), *referenced,
                                   *(m['id'] for m in reversed(messages))]))[:4]
    result, deadline = [], time.monotonic() + budget
    # 同一引用原图只读一次，结果不跨请求缓存，原图丢失后不会保留旧的可用状态。
    cache = {}
    async def get_message(mid, **options):
        if mid not in cache:
            cache[mid] = await ctx.message.get_by_id(mid, **options)
        return cache[mid]
    from types import SimpleNamespace
    scoped = SimpleNamespace(message=SimpleNamespace(get_by_id=get_message), chat=getattr(ctx, 'chat', None))
    for mid in selected:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            break
        try:
            images, sources = await asyncio.wait_for(read_images(scoped, session, mid, max_images=2), remaining)
        except asyncio.TimeoutError:
            break
        except (MediaUnavailable, OSError, ValueError, RuntimeError):
            continue
        original = cache.get(mid) or {}
        request = ' '.join(str(p.get('data', '')) for p in original.get('raw_message', []) if p.get('type') == 'text')[:500]
        # 引用关系来自真实消息段，不把模型提示中的 quote 字样当作授权。
        quoted = any(p.get('type') == 'reply' for p in original.get('raw_message', [])) and sources != [mid]
        record = {'message_id': mid, 'source_message_ids': sources,
                  'original_available': True, 'multiple_images': len(images) > 1,
                  'is_drawing_reference': bool(quoted and drawing.search(request)), 'request_text': request,
                  'request_author': original.get('message_info', {}).get('user_info', {}).get('user_nickname', '')}
        source = cache.get(sources[0]) or {}
        description = source.get('processed_plain_text')
        if isinstance(description, str) and description:
            record['source_description'] = description[:800]
        result.append(record)
    return result

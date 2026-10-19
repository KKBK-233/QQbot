"""同会话媒体读取：普通图片、引用、合并转发及明确的近期历史图片。"""

try:
    from .qq_media import recover_message
except ImportError:
    from qq_media import recover_message

class MediaUnavailable(ValueError):
    """仅携带固定可公开的缺图说明。"""


def media_parts(parts, max_images=2):
    """最多检查200个节点、四层转发；只接受宿主提供的图片字节。"""
    images, quotes = [], []
    queue = [(parts, 0)]
    checked = 0
    while queue and checked < 200:
        items, depth = queue.pop(0)
        if not isinstance(items, list):
            continue
        for part in items:
            checked += 1
            if checked > 200 or not isinstance(part, dict):
                break
            kind = part.get('type')
            if kind in ('image', 'emoji') and len(images) < max_images:
                if part.get('binary_data_base64'):
                    images.append(part['binary_data_base64'])
            elif kind == 'reply' and isinstance(part.get('data'), dict):
                target = part['data'].get('target_message_id')
                if target:
                    quotes.append(str(target))
            elif kind == 'forward' and depth < 4:
                nodes = part.get('data', [])
                if isinstance(nodes, list):
                    for node in nodes[:200 - checked]:
                        if isinstance(node, dict):
                            queue.append((node.get('content', []), depth + 1))
    return images, quotes


def has_media(parts):
    """历史列表不含字节，仅用结构筛选候选，选定后再读取原图。"""
    return any(isinstance(p, dict) and p.get('type') in ('image', 'emoji', 'forward') for p in parts)


async def read_images(ctx, session, message_id, max_images=2, history=False):
    """所有引用逐条核对所属群；无明确引用时仅选择同作者十五分钟内唯一候选。"""
    queue, visited = [(str(message_id), 0)], set()
    original = None
    had_quote = False
    while queue and len(visited) < 20:
        current, depth = queue.pop(0)
        if current in visited:
            continue
        visited.add(current)
        message = await ctx.message.get_by_id(current, chat_id=session, include_binary_data=True)
        if message and message.get('session_id') != session:
            continue
        # 本地没有消息或媒体字节时才补取，不替换已有的原图和引用关系。
        if not message or (has_media(message.get('raw_message', []))
                           and not media_parts(message.get('raw_message', []), max_images)[0]):
            recovered = await recover_message(ctx, session, current, max_images)
            if recovered and (not message or media_parts(recovered.get('raw_message', []), max_images)[0]):
                message = recovered
        if not message or message.get('session_id') != session:
            continue
        if current == str(message_id):
            original = message
        images, quotes = media_parts(message.get('raw_message', []), max_images)
        if images:
            return images, [current]
        had_quote = had_quote or bool(quotes)
        if depth < 2:
            queue.extend((target, depth + 1) for target in quotes)
    if history and original and not had_quote and hasattr(ctx.message, 'get_by_time_in_chat'):
        info = original.get('message_info', {}).get('user_info', {})
        author = info.get('user_id')
        stamp = float(original.get('timestamp') or 0)
        if author and stamp:
            recent = await ctx.message.get_by_time_in_chat(session, stamp - 900, stamp, limit=40, include_binary_data=False)
            # SDK将 capability payload 展开成消息列表，兼容直接返回字典的测试宿主。
            if isinstance(recent, dict):
                recent = recent.get('messages', [])
            candidates = [m for m in (recent or []) if isinstance(m, dict)
                and m.get('session_id') == session and str(m.get('message_id')) != str(message_id)
                and m.get('message_info', {}).get('user_info', {}).get('user_id') == author
                and 0 <= stamp - float(m.get('timestamp') or 0) <= 900
                and has_media(m.get('raw_message', []))]
            if len(candidates) == 1:
                return await read_images(ctx, session, candidates[0]['message_id'], max_images, history=False)
            if len(candidates) > 1:
                ids = ', '.join(str(m['message_id']) for m in candidates[:8])
                raise MediaUnavailable('近期有多条图片或聊天记录，候选消息编号：' + ids + '。请根据用户明确指向选择编号，无法确定时确认目标，不要要求重发已存在的原图。')
    raise MediaUnavailable('未取到指定消息、引用或明确历史消息的原图。转发记录若只剩摘要，需重新发送完整记录或原图；不能猜测图片特征。')

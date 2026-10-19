"""摘要模型只选择真人原句，摘要正文由服务器重建，防止机器人自述污染。"""
from src.services.person_memory_v2 import message_actor
from src.services.memory_evidence_policy import canonical_record


def user_catalog(messages):
    """忽略机器人；显式绑定消息ID和发言人，避免昵称导致事实串人。"""
    import json
    rows = []
    for message in messages:
        actor = message_actor(message)
        if actor and message.message_id:
            rows.append({'message_id': str(message.message_id), 'sender': actor[1],
                         'text': str(message.processed_plain_text or '')})
    return json.dumps(rows, ensure_ascii=False)


def grounded_summary(data, messages):
    """未知证据直接拒绝整批；模型的summary、实体和关系不直接进入存储。"""
    by_id = {str(item.message_id): item for item in messages if item.message_id and message_actor(item)}
    facts = data.get('facts', [])
    if not isinstance(facts, list):
        raise ValueError('摘要事实数组格式无效')
    lines, proven = [], []
    for fact in facts:
        if not isinstance(fact, dict):
            raise ValueError('摘要事实缺少证据')
        mid, quote = str(fact.get('message_id', '')), str(fact.get('quote', ''))
        message = by_id.get(mid)
        if message is None:
            raise ValueError('摘要证据不是窗口内真人消息')
        actor = message_actor(message)
        record = canonical_record(message.processed_plain_text, quote, actor[1], str(fact.get('kind', 'interaction')))
        if record is None:
            raise ValueError('摘要证据不完整或属于不应记忆的内容')
        if any(str(e.get('message_id', '')) != mid for e in fact.get('image_evidence', []) if isinstance(e, dict)):
            raise ValueError('图片证据与原句不是同一条真人消息')
        if record['text'] not in lines:
            lines.append(record['text'])
            proven.append({**fact, 'text': record['text'], 'fact_id': str(fact.get('fact_id') or f'f{len(proven)+1}')})
    if str(data.get('summary', '')).strip() and not lines:
        raise ValueError('非空摘要没有真人原句证据')
    return '\n'.join(lines), proven

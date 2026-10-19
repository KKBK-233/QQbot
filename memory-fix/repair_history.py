"""一次性重建旧人物证据并撤回已确认污染，原始数据库留备份且可恢复删除。"""
import asyncio
from datetime import datetime, timezone
import json
import re
import sqlite3

from src.A_memorix.host_service import a_memorix_host_service
from src.common.message_repository import find_messages
from src.services.memory_service import memory_service
from src.services.person_memory_v2 import message_actor, save_evidence
from src.services.memory_withdraw_v2 import withdraw_paragraph


def snapshot():
    """只迁移自动写回人物段落；手工资料、角色参考图和新证据记录均保留。"""
    root = a_memorix_host_service.get_runtime_data_dir()
    db = root / 'metadata/metadata.db'
    backup = root / 'repair-v2-before.sqlite'
    with sqlite3.connect(f'{db.as_uri()}?mode=ro', uri=True) as conn:
        if not backup.exists():
            with sqlite3.connect(backup) as destination:
                conn.backup(destination)
        conn.row_factory = sqlite3.Row
        return [dict(row) for row in conn.execute('SELECT hash,source,content,metadata FROM paragraphs WHERE COALESCE(is_deleted,0)=0')]


async def repair_once():
    """用作者自己的原消息替换旧模型概括；不能验证的旧自动画像撤回，绝不直接晋升。"""
    root = a_memorix_host_service.get_runtime_data_dir()
    state_path = root / 'repair-v2-state.json'
    state = json.loads(state_path.read_text()) if state_path.exists() else {'done': [], 'saved': 0, 'withdrawn': 0}
    if state.get('complete') and state.get('people_backfill_complete'):
        return state
    rows = await asyncio.to_thread(snapshot)
    for row in rows:
        if row['hash'] in state['done']:
            continue
        metadata = json.loads(row['metadata'] or '{}')
        if metadata.get('evidence_policy') == 2:
            continue
        is_person = row['source'].startswith('person_fact:') and metadata.get('writeback_source') == 'memory_flow_service'
        # 历史错误及明确不用保存的测试细节仅在此次迁移中处理，不改变后续通用学习规则。
        polluted = row['source'].startswith('chat_summary:') and re.search(
            r'黑长直|棕色瞳|棕瞳|青绿色长发|耳机耳罩|指甲盖|指尖红|手指尖|肩膀尖', row['content'])
        if not is_person and not polluted:
            continue
        if is_person:
            pid = str(metadata.get('person_id', ''))
            session_id = str(metadata.get('chat_id', ''))
            for mid in metadata.get('evidence_message_ids', [])[:6]:
                messages = find_messages(session_id=session_id, message_id=str(mid), limit=2)
                if len(messages) != 1 or not message_actor(messages[0]) or message_actor(messages[0])[0] != pid:
                    continue
                text = str(messages[0].processed_plain_text or '').strip()
                # 不补录用户用来暴露缺陷的生图局部细节。
                if re.search(r'指甲|指尖|手指尖|肩膀尖', text):
                    continue
                outcome = await save_evidence(session_id, str(mid), text, 'self')
                if outcome['success']:
                    state['saved'] += 1
                elif outcome.get('error') not in ('原句不完整、被否定或不适合保存', '原消息不存在或不唯一', '该旧事实已撤回，不再自动写入'):
                    raise RuntimeError('历史原句重建失败，保留旧记录等待重试')
        removed = await withdraw_paragraph(row['hash'], 'administrator', 'evidence_v2_history_repair')
        if not removed.get('success'):
            raise RuntimeError('旧自动记忆撤回失败，迁移暂停')
        state['withdrawn'] += 1
        state['done'].append(row['hash'])
        temp = state_path.with_suffix('.tmp')
        temp.write_text(json.dumps(state, ensure_ascii=False), encoding='utf-8')
        temp.replace(state_path)
    # 旧版只为最后回复对象积累画像，补回其它真人最近的实际交流原句。
    if not state.get('people_backfill_complete'):
        sessions = {str(json.loads(row['metadata'] or '{}').get('chat_id', '')) for row in rows}
        for session_id in sorted(sessions - {''}):
            messages = find_messages(session_id=session_id, limit=120, limit_mode='latest', filter_bot=True)
            by_person = {}
            for message in reversed(messages):
                actor = message_actor(message)
                text = str(message.processed_plain_text or '').strip()
                if not actor or not 12 <= len(text) <= 280 or re.search(r'指甲|指尖|手指尖|肩膀尖', text):
                    continue
                if len(by_person.setdefault(actor[0], [])) < 3:
                    by_person[actor[0]].append(message)
            for group in by_person.values():
                for message in group:
                    outcome = await save_evidence(session_id, str(message.message_id), str(message.processed_plain_text).strip(), 'self')
                    if outcome['success']:
                        state['saved'] += 1
        state['people_backfill_complete'] = True
    state['complete'] = True
    state['finished_at'] = datetime.now(timezone.utc).isoformat()
    state_path.write_text(json.dumps(state, ensure_ascii=False), encoding='utf-8')
    from src.common.logger import get_logger
    get_logger('memory_flow_service').info(f'历史记忆修复完成: saved={state["saved"]} withdrawn={state["withdrawn"]}')
    return state

"""普通聊天的明确记忆与修正工具，保存真实原句并可撤回同一用户的旧记录。"""
import json
import re
import sqlite3

from src.A_memorix.host_service import a_memorix_host_service
from src.common.message_repository import find_messages
from src.config.config import global_config
from src.core.tooling import ToolSpec
from src.services.person_memory_v2 import message_actor, save_evidence
from src.services.memory_service import memory_service
from src.services.memory_withdraw_v2 import withdraw_paragraph

INTENT = re.compile(r'记住|记一下|记下来|记得|记忆|更正|纠正|改成|改为|忘掉|忘记|不再记')


def get_tool_spec():
    """只保存当事人的原话，模型不能自由编造记忆正文。"""
    return ToolSpec(name='remember_memory', provider_name='maisaka_builtin', provider_type='builtin',
                    enabled=bool(global_config.a_memorix.plugin.enabled),
                    description='用户明确要求记住或更正时调用。message_id取真人原消息，quote逐字摘录完整原句；'
                                'replaces_hash只用于用户明确更正且属于该用户的旧记忆。只有success=true才能说已记住。',
                    parameters_schema={'type': 'object', 'properties': {
                        'message_id': {'type': 'string', 'description': '当前会话真人提出记忆要求的消息ID'},
                        'quote': {'type': 'string', 'maxLength': 500, 'description': '逐字完整原句，不改写、不写机器人回复'},
                        'replaces_hash': {'type': 'string', 'description': '可选：query_memory返回的被用户更正的旧段落hash'}},
                        'required': ['message_id', 'quote']})


def owned_paragraph(hash_value, person_id):
    """只读核验段落作者；不能用群内权限更改其他成员的个人记忆。"""
    if not re.fullmatch(r'[a-f0-9]{64}', hash_value):
        return False
    path = a_memorix_host_service.get_runtime_data_dir() / 'metadata/metadata.db'
    with sqlite3.connect(f'{path.as_uri()}?mode=ro', uri=True) as conn:
        row = conn.execute('SELECT metadata,is_deleted FROM paragraphs WHERE hash=?', (hash_value,)).fetchone()
    if not row or row[1]:
        return False
    return json.loads(row[0] or '{}').get('person_id') == person_id


async def handle_tool(tool_ctx, invocation, context=None):
    """落库失败不承诺成功；更正先保存新内容，再通过原生可恢复删除撤回旧证据。"""
    session_id = str(tool_ctx.runtime.session_id)
    message_id = str(invocation.arguments.get('message_id', ''))
    quote = str(invocation.arguments.get('quote', ''))
    old_hash = str(invocation.arguments.get('replaces_hash', '') or '')
    messages = find_messages(session_id=session_id, message_id=message_id, limit=2)
    if len(messages) != 1 or not INTENT.search(str(messages[0].processed_plain_text or '')):
        return tool_ctx.build_failure_result(invocation.tool_name, '需要同会话真人明确提出记住或更正的原消息')
    actor = message_actor(messages[0])
    if actor is None or (old_hash and not owned_paragraph(old_hash, actor[0])):
        return tool_ctx.build_failure_result(invocation.tool_name, '旧记忆不属于这位发言者，不能替换')
    outcome = await save_evidence(session_id, message_id, quote, 'note')
    if not outcome['success']:
        return tool_ctx.build_failure_result(invocation.tool_name, outcome['error'], structured_content=outcome)
    if old_hash and old_hash not in outcome['stored_ids']:
        removed = await withdraw_paragraph(old_hash, actor[0], 'explicit_user_memory_correction')
        if not removed.get('success'):
            return tool_ctx.build_failure_result(invocation.tool_name, '新记忆已保存，但旧记忆撤回失败，不要宣称更正完成',
                                                  structured_content={**outcome, 'correction_complete': False})
    return tool_ctx.build_success_result(invocation.tool_name, '原话已保存到共享长期记忆。', structured_content=outcome)

"""真实原库只读回填索引，并隔离验证工具注册、名单注入与历史消息补充。"""
import asyncio
from contextlib import closing, redirect_stdout, redirect_stderr
import io
import json
import os
from pathlib import Path
import sqlite3
import sys
from types import SimpleNamespace
from unittest.mock import patch


async def verify():
    """使用生产数据核对覆盖率；画像服务替换为固定空结果，不调用模型。"""
    from src.services.person_memory_index import get_runtime_index, directory_reference
    from src.services.bot_account_service import is_bot_self
    from src.maisaka.builtin_tool import get_builtin_tools, list_known_people, query_person_profile
    from src.maisaka.builtin_tool.context import BuiltinToolRuntimeContext
    from src.maisaka.memory import person_profile
    from src.common.database.database import _DB_FILE
    index = get_runtime_index()
    index.refresh(force=True)
    group_id = '1022382380'
    current = index.list_people(platform='qq', group_id=group_id)
    with closing(sqlite3.connect(_DB_FILE)) as chat:
        expected = {(p, u) for p, u in chat.execute('SELECT DISTINCT platform,user_id FROM mai_messages WHERE group_id=?',
            (group_id,)) if p and u and not is_bot_self(p, u)}
    actual = {(p['platform'], p['user_id']) for p in current['people']}
    assert actual == expected and len(current['people']) > 2, '人物索引未覆盖全部历史真人'
    stream = SimpleNamespace(platform='qq', group_id=group_id, user_id='')
    ctx = SimpleNamespace(runtime=SimpleNamespace(chat_stream=stream),
        build_success_result=BuiltinToolRuntimeContext.build_success_result,
        build_failure_result=BuiltinToolRuntimeContext.build_failure_result)
    invocation = SimpleNamespace(tool_name='list_known_people', arguments={})
    result = await list_known_people.handle_tool(ctx, invocation)
    assert result.success and result.structured_content['total'] == len(expected)
    assert result.metadata['replyer_memory_reference']
    definitions = get_builtin_tools()
    assert any('list_known_people' in str(t) for t in definitions), '名单工具没有暴露给规划器'
    anchor = SimpleNamespace(platform='qq', message_info=SimpleNamespace(
        group_info=SimpleNamespace(group_id=group_id)))
    reference = await directory_reference(anchor)
    assert all(json.dumps(p['name'][:60], ensure_ascii=False) in reference for p in current['people'])
    with patch.object(person_profile, 'collect_person_profile_candidates', return_value=[]):
        injected = await person_profile.build_person_profile_injection_messages(anchor_message=anchor)
        assert injected and '本群人物索引' in injected[0]
    # 空画像也能按账号找到原始消息；定向查询不会把同名者或私聊内容混入当前群。
    target = next(p for p in current['people'] if p['memory_count'] == 0 and p['recent_messages'])
    async def empty_profile(**kwargs):
        return {'success': False, 'person_id': kwargs['person_id'], 'error': '无画像'}
    invocation = SimpleNamespace(tool_name='query_person_profile', arguments={'person_id': target['person_id']})
    with patch.object(query_person_profile.memory_service, 'profile_admin', empty_profile):
        result = await query_person_profile.handle_tool(ctx, invocation)
    assert result.success and result.structured_content['success']
    assert result.structured_content['historical_messages'] and '历史消息证据' in result.content
    assert result.content.count('## 历史消息证据') == 1
    assert result.structured_content['profile_source'] == 'message_history'
    invocation.arguments = {'person_name': target['name']}
    with patch.object(query_person_profile.memory_service, 'profile_admin', empty_profile):
        by_name = await query_person_profile.handle_tool(ctx, invocation)
    assert by_name.success and by_name.structured_content['person_id'] == target['person_id']
    # 同一部署脚本再跑一次不变更文件，避免与以前的记忆修复补丁互相覆盖。
    source = Path(__file__).resolve().parent
    sys.path.insert(0, str(source))
    from deploy_index import patches
    root = Path('/home/qqbot/QQbot/MaiBot')
    assert all((root / p).read_text(encoding='utf-8') == s for p, s in patches(root).items())
    from deploy import patches as evidence_patches
    assert all((root / p).read_text(encoding='utf-8') == s for p, s in evidence_patches(root).items())
    with closing(sqlite3.connect(index.index_db)) as conn:
        total_people = conn.execute('SELECT count(*) FROM people').fetchone()[0]
        indexed_memories = conn.execute('SELECT count(distinct hash) FROM person_paragraphs').fetchone()[0]
        linked_people = conn.execute('SELECT count(distinct person_id) FROM person_paragraphs').fetchone()[0]
    return {'success': True, 'indexed_people_all_chats': total_people, 'historical_people_in_target_group': len(expected),
        'listed_people_in_target_group': len(current['people']),
        'people_with_message_evidence_in_target_group': sum(bool(p['recent_messages']) for p in current['people']),
        'people_with_linked_memory_in_target_group': sum(p['memory_count'] > 0 for p in current['people']),
        'indexed_memory_paragraphs': indexed_memories, 'people_with_linked_memory_all_chats': linked_people,
        'directory_injection': True, 'directory_tool_registered': True, 'empty_profile_message_fallback': True,
        'name_query_uses_indexed_account': True,
        'deploy_idempotent': True, 'evidence_deploy_compatible': True, 'model_calls': 0, 'qq_messages_sent': 0}


if __name__ == '__main__':
    os.chdir('/home/qqbot/QQbot/MaiBot')
    sys.path.insert(0, '/home/qqbot/QQbot/MaiBot')
    with redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()):
        result = asyncio.run(verify())
    print(json.dumps(result, ensure_ascii=False, indent=2))

"""用真实原生存储和画像投影验证修复；隔离临时库，不调用模型或QQ。"""
import asyncio
import contextlib
import importlib.util
import io
from pathlib import Path
import sys
import tempfile
from types import SimpleNamespace
from unittest.mock import patch


async def verify():
    """验证作者隔离、模型捏造、失败状态、重复写入及可注入画像。"""
    from src.A_memorix.core.storage.metadata_store import MetadataStore
    from src.A_memorix.core.runtime.services.ingest_service import MemoryIngestService
    from src.A_memorix.core.utils.person_profile_service import PersonProfileService
    from src.A_memorix.core.utils.profile_text import build_profile_injection_text
    from src.services.memory_service import MemoryWriteResult
    source = Path(__file__).resolve().parent
    for name, filename in [('memory_evidence_policy', 'evidence_policy.py'), ('person_memory_v2', 'person_memory.py'),
                           ('summary_evidence_v2', 'summary_evidence.py')]:
        spec = importlib.util.spec_from_file_location('src.services.' + name, source / filename)
        module = importlib.util.module_from_spec(spec)
        sys.modules[spec.name] = module
        spec.loader.exec_module(module)
    from src.services import person_memory_v2 as memory
    def message(mid, uid, text):
        return SimpleNamespace(message_id=mid, user_id=uid, platform='test', timestamp=100,
                               processed_plain_text=text, message_info=SimpleNamespace(user_info=SimpleNamespace(user_id=uid)))
    rows = {'1': message('1', 'alice', '我喜欢猫。我使用Linux。'),
            '2': message('2', 'bob', '帮我查一下阿隆索的比赛结果'),
            '3': message('3', 'bot', '我住在月球。'),
            '4': message('4', 'alice', '记住。我希望回答简短。'),
            '5': message('5', 'alice', '更正。我希望回答详细。')}
    with tempfile.TemporaryDirectory() as directory:
        store = MetadataStore(data_dir=Path(directory) / 'metadata')
        store.connect()
        writer = MemoryIngestService(SimpleNamespace(metadata_store=store))
        async def ingest(**kwargs):
            mid = store.add_paragraph(content=kwargs['text'], source='person_fact:' + kwargs['person_ids'][0], metadata=kwargs['metadata'])
            writer._write_person_fact_claims(paragraph_hash=mid, content=kwargs['text'],
                                            person_ids=kwargs['person_ids'], metadata=kwargs['metadata'], timestamp=100)
            return MemoryWriteResult(success=True, stored_ids=[mid])
        def find(**kwargs):
            return [rows[kwargs['message_id']]] if kwargs['session_id'] == 's1' and kwargs['message_id'] in rows else []
        with patch.object(memory, 'find_messages', find), patch.object(memory, 'is_bot_self', lambda p, u: u == 'bot'), \
             patch.object(memory, 'get_person_id', lambda p, u: p + ':' + u), \
             patch.object(memory, 'Person', lambda person_id: SimpleNamespace(is_known=True, person_name=person_id, nickname='')), \
             patch.object(memory.memory_service, 'ingest_text', ingest):
            outcome = await memory.save_evidence('s1', '1', '我喜欢猫。', 'self')
            assert outcome['success'] and outcome['section'] == 'stable_facts'
            await memory.save_evidence('s1', '1', '我喜欢猫。', 'self')
            assert len(store.list_current_person_fact_claims('test:alice')) == 1
            assert not (await memory.save_evidence('s1', '1', '我喜欢狗。', 'self'))['success']
            assert not (await memory.save_evidence('s2', '1', '我喜欢猫。', 'self'))['success']
            assert not (await memory.save_evidence('s1', '3', '我住在月球。', 'self'))['success']
            assert not (await memory.save_evidence('s1', '1', '我喜欢猫。', 'self', allowed_ids={'2'}))['success']
            assert (await memory.save_evidence('s1', '2', rows['2'].processed_plain_text))['success']
            claims = store.list_person_profile_fact_claims('test:bob', effective_at=100, limit=10)
            assert len(claims) == 1 and claims[0]['profile_section'] == 'recent_interactions'
            async def failed(**kwargs):
                return MemoryWriteResult(success=False, detail='isolated_failure')
            with patch.object(memory.memory_service, 'ingest_text', failed):
                assert not (await memory.save_evidence('s1', '1', '我使用Linux。', 'self'))['success']
            from src.services.summary_evidence_v2 import grounded_summary, user_catalog
            assert '月球' not in user_catalog(list(rows.values()))
            text, facts = grounded_summary({'summary': '错误的自由概括', 'facts': [
                {'message_id': '1', 'quote': '我喜欢猫。', 'kind': 'self'}]}, list(rows.values()))
            assert text == 'test:alice喜欢猫' and '错误' not in text
            for fact in [{'message_id': '3', 'quote': '我住在月球。'},
                         {'message_id': '1', 'quote': '我喜欢狗。'}, {'message_id': 'outside', 'quote': '我喜欢猫。'}]:
                try:
                    grounded_summary({'summary': '有新增', 'facts': [fact]}, list(rows.values()))
                    raise AssertionError('接受了不可信摘要')
                except ValueError:
                    pass
            from src.maisaka.builtin_tool import remember_memory as tool
            from src.maisaka.builtin_tool.context import BuiltinToolRuntimeContext
            ctx = SimpleNamespace(runtime=SimpleNamespace(session_id='s1'),
                                  build_success_result=BuiltinToolRuntimeContext.build_success_result,
                                  build_failure_result=BuiltinToolRuntimeContext.build_failure_result)
            removed = []
            async def delete(**kwargs):
                removed.append(kwargs)
                return {'success': True}
            async def retract(**kwargs):
                store.retract_fact_claim(kwargs['claim_id'], reason='isolated_test')
                return {'success': True}
            with patch.object(tool, 'find_messages', find), \
                 patch.object(tool.a_memorix_host_service, 'get_runtime_data_dir', lambda: Path(directory)), \
                 patch.object(tool.memory_service, 'delete_admin', delete), \
                 patch.object(tool.memory_service, 'fact_admin', retract):
                invocation = SimpleNamespace(tool_name='remember_memory', arguments={'message_id': '4', 'quote': '我希望回答简短。'})
                result = await tool.handle_tool(ctx, invocation)
                assert result.success and result.structured_content['stored_ids']
                old_hash = result.structured_content['stored_ids'][0]
                assert not tool.owned_paragraph(old_hash, 'test:bob')
                invocation.arguments = {'message_id': '5', 'quote': '我希望回答详细。', 'replaces_hash': old_hash}
                assert (await tool.handle_tool(ctx, invocation)).success
                assert removed[0]['selector'] == {'hashes': [old_hash]}
                assert all('简短' not in c['value_text'] for c in store.list_current_person_fact_claims('test:alice'))
                assert not (await memory.save_evidence('s1', '4', '我希望回答简短。', 'self'))['success']
                invocation.arguments = {'message_id': '1', 'quote': '我喜欢猫。'}
                assert not (await tool.handle_tool(ctx, invocation)).success
                invocation.arguments = {'message_id': '4', 'quote': '我希望回答简短。'}
                with patch.object(memory.memory_service, 'ingest_text', failed):
                    assert not (await tool.handle_tool(ctx, invocation)).success
        profile = PersonProfileService(metadata_store=store)
        # 不启用画像分类模型，确认账本本身即可生成自动注入的稳定事实与互动。
        async def no_model(**kwargs):
            return {key: [] for key in ('identity_settings', 'relationship_settings', 'stable_facts',
                                        'interaction_preferences', 'recent_interactions', 'uncertain_notes')}
        with patch.object(profile, '_classify_profile_evidence', no_model):
            for pid, expected in [('test:alice', '喜欢猫'), ('test:bob', '阿隆索')]:
                payload = await profile.query_person_profile(person_id=pid, force_refresh=True)
                assert expected in build_profile_injection_text(payload['profile_text'], include_uncertain_fallback=False)
        store.close()
    return '真实临时库：作者/会话/原句校验、幂等、画像注入、摘要证据、记住/更正工具与失败回报通过'


if __name__ == '__main__':
    sys.path.insert(0, '/home/qqbot/QQbot/MaiBot')
    # 宿主导入日志可能包含配置，统一捕获，仅输出固定验证摘要。
    with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
        result = asyncio.run(verify())
    print(result)

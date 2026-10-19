"""有备份、幂等、锚点校验的原生记忆修复部署，保留既有源码改动。"""
import argparse
from datetime import datetime, timezone
from pathlib import Path
import shutil

SOURCE = Path(__file__).resolve().parent


def replace_once(text, old, new):
    """只允许已知版本的唯一锚点，部署过的补丁直接保持。"""
    if new in text:
        return text
    if text.count(old) != 1:
        raise ValueError('源码锚点缺失或重复：' + old[:80])
    return text.replace(old, new, 1)


def patches(root):
    """人物画像第一阶段：批量原句证据写回，不改变模型、人设和共享范围。"""
    path = 'src/services/memory_flow_service.py'
    text = (root / path).read_text(encoding='utf-8')
    start = text.index('    async def _handle_message(self, message: Any) -> None:')
    end = text.index('    def _resolve_target_person(', start)
    text = text[:start] + '''    async def _handle_message(self, message: Any) -> None:
        # 批量核验原消息，稳定自述与互动经历分别进入原生人物画像。
        from src.services.person_memory_v2 import writeback
        await writeback(self, message)

''' + text[end:]
    fact_path = 'src/A_memorix/core/storage/metadata_fact.py'
    facts = (root / fact_path).read_text(encoding='utf-8')
    facts = replace_once(facts, """                    OR (
                        stability = 'uncertain'
                        AND authority = 'summary_derived'
                        AND profile_section = 'uncertain_notes'
                    )""", """                    OR (
                        stability = 'temporal'
                        AND authority IN ('manual', 'direct_user', 'imported')
                        AND profile_section = 'recent_interactions'
                    )
                    OR (
                        stability = 'uncertain'
                        AND authority = 'summary_derived'
                        AND profile_section = 'uncertain_notes'
                    )""")
    # 仅修改画像投影排序，不改稳定事实接口的时间和状态规则。
    pos = facts.index('    def list_person_profile_fact_claims(')
    tail = facts[pos:]
    tail = replace_once(tail, '                fact_key ASC,\n                value_normalized ASC,\n                claim_id ASC\n            LIMIT ?',
                        "                CASE WHEN profile_section = 'recent_interactions' THEN last_confirmed_at END DESC,\n                fact_key ASC,\n                value_normalized ASC,\n                claim_id ASC\n            LIMIT ?")
    facts = facts[:pos] + tail
    contents = {path: text, fact_path: facts,
            'src/services/memory_evidence_policy.py': (SOURCE / 'evidence_policy.py').read_text(encoding='utf-8'),
            'src/services/person_memory_v2.py': (SOURCE / 'person_memory.py').read_text(encoding='utf-8')}
    if (SOURCE / 'remember_memory.py').exists():
        contents['src/maisaka/builtin_tool/remember_memory.py'] = (SOURCE / 'remember_memory.py').read_text(encoding='utf-8')
        contents['src/services/summary_evidence_v2.py'] = (SOURCE / 'summary_evidence.py').read_text(encoding='utf-8')
        contents['src/services/repair_memory_history_v2.py'] = (SOURCE / 'repair_history.py').read_text(encoding='utf-8')
        contents['src/services/memory_withdraw_v2.py'] = (SOURCE / 'memory_withdraw.py').read_text(encoding='utf-8')
        contents[path] = replace_once(contents[path], '                cursor = str(state.get("cursor", "") or "")',
                                      '                from src.services.repair_memory_history_v2 import repair_once\n                await repair_once()\n                cursor = str(state.get("cursor", "") or "")')
        contents['prompts/zh-CN/a_memorix_chat_summary.prompt'] = (SOURCE / 'summary.prompt').read_text(encoding='utf-8')
        tool_path = 'src/maisaka/builtin_tool/__init__.py'
        tools = (root / tool_path).read_text(encoding='utf-8')
        tools = replace_once(tools, 'from .query_memory import get_tool_spec as get_query_memory_tool_spec',
                             'from .remember_memory import get_tool_spec as get_remember_memory_tool_spec\nfrom .remember_memory import handle_tool as handle_remember_memory_tool\nfrom .query_memory import get_tool_spec as get_query_memory_tool_spec')
        tools = replace_once(tools, '    BuiltinToolEntry("query_memory", _get_query_memory_tool_spec, handle_query_memory_tool, stage="action"),',
                             '    BuiltinToolEntry("remember_memory", get_remember_memory_tool_spec, handle_remember_memory_tool, stage="action"),\n    BuiltinToolEntry("query_memory", _get_query_memory_tool_spec, handle_query_memory_tool, stage="action"),')
        contents[tool_path] = tools
        summary_path = 'src/A_memorix/core/utils/summary_importer.py'
        summary = (root / summary_path).read_text(encoding='utf-8')
        summary = replace_once(summary, '            chat_history_text = message_api.build_readable_messages(messages)',
                               '            from src.services.summary_evidence_v2 import user_catalog, grounded_summary\n            chat_history_text = user_catalog(messages)\n            metadata = {**(metadata or {}), "evidence_policy": 2}')
        summary = replace_once(summary, '            summary_text = raw_summary.strip()\n            entities = _normalize_entity_items(data.get("entities"))\n            relations = _normalize_relation_items(data.get("relations"))\n            facts = _normalize_summary_facts(data.get("facts"))',
                               '            # 原句校验后重建摘要，丢弃模型自由概括的事实值与关系。\n            summary_text, evidence_facts = grounded_summary(data, messages)\n            entities = []\n            relations = []\n            facts = _normalize_summary_facts(evidence_facts)\n            metadata["evidence_message_ids"] = [str(item["message_id"]) for item in evidence_facts]')
        summary = replace_once(summary, '            paragraph_metadata = coerce_metadata_dict(paragraph.get("metadata"))\n            memory_change = (',
                               '            paragraph_metadata = coerce_metadata_dict(paragraph.get("metadata"))\n            if paragraph_metadata.get("evidence_policy") != 2:\n                continue\n            memory_change = (')
        contents[summary_path] = summary
        loop_path = 'src/maisaka/chat_loop_service.py'
        loop = (root / loop_path).read_text(encoding='utf-8')
        loop = replace_once(loop, '不要把私聊或个人隐私记忆带到群聊里。',
                            '当前管理员已开启群聊和私聊完全共享，可检索同一用户在其他会话的记忆；按账号区分人物。明确要求记住或更正时调用remember_memory，成功前不要说已记住。')
        contents[loop_path] = loop
    return contents


def main():
    """先准备全部补丁，语法校验后才备份并写入。"""
    parser = argparse.ArgumentParser()
    parser.add_argument('--root', type=Path, default=Path('/home/qqbot/QQbot/MaiBot'))
    args = parser.parse_args()
    contents = patches(args.root)
    for relative, text in contents.items():
        if relative.endswith('.py'):
            compile(text, relative, 'exec')
    stamp = datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')
    for relative, text in contents.items():
        path = args.root / relative
        if path.exists() and path.read_text(encoding='utf-8') == text:
            continue
        backup = args.root / 'data/memory-fix-backups' / stamp / relative
        if path.exists():
            backup.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(path, backup)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding='utf-8')
        assert path.read_text(encoding='utf-8') == text
        print('已部署：' + relative)


if __name__ == '__main__':
    main()

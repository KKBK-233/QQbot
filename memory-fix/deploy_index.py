"""带锚点、备份和语法检查的人物索引部署，覆盖完整历史而不变更模型。"""
import argparse
from datetime import datetime, timezone
from pathlib import Path
import shutil

from deploy import replace_once

SOURCE = Path(__file__).resolve().parent


def patches(root):
    """注册名单工具、自动注入轻量目录，并为人物画像补充有来源的历史消息。"""
    contents = {
        'src/services/person_memory_index.py': (SOURCE / 'person_index.py').read_text(encoding='utf-8'),
        'src/maisaka/builtin_tool/list_known_people.py': (SOURCE / 'list_known_people.py').read_text(encoding='utf-8')}
    path = 'src/maisaka/builtin_tool/__init__.py'
    text = (root / path).read_text(encoding='utf-8')
    text = replace_once(text, 'from .query_person_profile import get_tool_spec as get_query_person_profile_tool_spec',
        'from .list_known_people import get_tool_spec as get_list_known_people_tool_spec\n'
        'from .list_known_people import handle_tool as handle_list_known_people_tool\n'
        'from .query_person_profile import get_tool_spec as get_query_person_profile_tool_spec')
    entry = '    BuiltinToolEntry("list_known_people", get_list_known_people_tool_spec, handle_list_known_people_tool, stage="action"),\n'
    anchor = '    BuiltinToolEntry("remember_memory", get_remember_memory_tool_spec, handle_remember_memory_tool, stage="action"),'
    # 保留旧部署器依赖的remember_memory/query_memory连续锚点，避免重复注册。
    if entry + anchor not in text and entry in text:
        if text.count(entry) != 1:
            raise ValueError('人物目录工具注册重复')
        text = text.replace(entry, '', 1)
    text = replace_once(text, anchor, entry + anchor)
    contents[path] = text
    path = 'src/maisaka/memory/person_profile.py'
    text = (root / path).read_text(encoding='utf-8')
    text = replace_once(text, '    if not candidates:\n        return []', '''    # 轻量人物目录覆盖全历史，不受详细画像候选人数上限限制。
    directory_messages = []
    try:
        from src.services.person_memory_index import directory_reference
        directory = await directory_reference(anchor_message)
        if directory:
            directory_messages.append(directory)
    except Exception:
        logger.warning("人物索引注入失败，保留现有画像；不能据此判断只认识近期发言者")
    if not candidates:
        return directory_messages''')
    text = replace_once(text, '    return [reference_block] if reference_block else []',
        '    return directory_messages + ([reference_block] if reference_block else [])')
    contents[path] = text
    path = 'src/maisaka/builtin_tool/query_person_profile.py'
    text = (root / path).read_text(encoding='utf-8')
    text = replace_once(text, 'from typing import Any, Dict, Optional', 'import asyncio\nimport json\nfrom typing import Any, Dict, Optional')
    text = replace_once(text, '    limit = _normalize_limit(invocation.arguments.get("limit"))', '''    # 昵称先在当前人物索引解析；同名账号不能交给全局模糊查询任取第一条。
    if not person_id and person_name:
        try:
            from src.services.person_memory_index import get_runtime_index
            stream = tool_ctx.runtime.chat_stream
            resolved = await asyncio.to_thread(get_runtime_index().resolve_person, person_name,
                platform=str(stream.platform or ""), group_id=str(stream.group_id or ""),
                user_id=str(stream.user_id or ""))
            if resolved["ambiguous"]:
                return tool_ctx.build_failure_result(invocation.tool_name,
                    "该名称对应多位历史发言者，请按候选person_id分别查询，不能把同名者混成一人。",
                    structured_content=resolved)
            person_id = resolved["person_id"]
        except Exception:
            pass
    limit = _normalize_limit(invocation.arguments.get("limit"))''')
    text = replace_once(text, '    if not bool(payload.get("success")):\n        error_message', '''    # 目录按账号回查历史消息；画像空白时也能返回有来源的互动证据。
    evidence = []
    try:
        from src.services.person_memory_index import get_runtime_index
        stream = tool_ctx.runtime.chat_stream
        evidence = await asyncio.to_thread(get_runtime_index().recent_messages,
            structured_content["person_id"], platform=str(stream.platform or ""),
            group_id=str(stream.group_id or ""), user_id=str(stream.user_id or ""), limit=4)
    except Exception:
        pass
    structured_content["historical_messages"] = evidence
    if not bool(payload.get("success")) and not evidence:
        error_message''')
    evidence_block = r'''    if evidence:
        profile_text += "\n\n## 历史消息证据（不等于稳定性格；媒体描述可能来自识别）\n"
        profile_text += "\n".join(
            f"- message_id={item['message_id']}，文本片段={json.dumps(item['text_excerpt'], ensure_ascii=False)}"
            for item in evidence)
        structured_content["success"] = True
        if not payload.get("success"):
            structured_content["profile_source"] = "message_history"
        structured_content["summary"] = profile_text
    return tool_ctx.build_success_result(
        invocation.tool_name,
        profile_text,'''
    # 兼容本轮已部署的早期版本；更新证据标记时不能重复追加整段历史。
    old_evidence_block = evidence_block.replace(
        '        if not payload.get("success"):\n            structured_content["profile_source"] = "message_history"\n', '')
    if old_evidence_block in text:
        text = replace_once(text, old_evidence_block, evidence_block)
    else:
        text = replace_once(text, '    return tool_ctx.build_success_result(\n        invocation.tool_name,\n        profile_text,', evidence_block)
    contents[path] = text
    return contents


def main():
    """全部补丁先检查，变化文件才备份；支持预检查与幂等部署。"""
    parser = argparse.ArgumentParser()
    parser.add_argument('--root', type=Path, default=Path('/home/qqbot/QQbot/MaiBot'))
    parser.add_argument('--check', action='store_true')
    args = parser.parse_args()
    contents = patches(args.root)
    for relative, text in contents.items():
        compile(text, relative, 'exec')
    if args.check:
        print(f'索引补丁预检查通过：{len(contents)}个文件')
        return
    stamp = datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')
    for relative, text in contents.items():
        path = args.root / relative
        if path.exists() and path.read_text(encoding='utf-8') == text:
            continue
        if path.exists():
            backup = args.root / 'data/memory-index-backups' / stamp / relative
            backup.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(path, backup)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding='utf-8')
        print('已部署：' + relative)


if __name__ == '__main__':
    main()

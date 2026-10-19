"""只调整宿主即时提示和已知固定规则布局，验证后再切换DeepSeek会话模型。"""
import argparse
from copy import deepcopy
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import shutil
import sqlite3
import sys

ROOT = Path('/home/qqbot/QQbot/MaiBot')
SOURCE = Path('/mnt/d/code/web/QQbot')
MARKER = '【宿主本轮上下文】'

def replace_once(text, old, new):
    """幂等补丁：锚点变化即停止，避免覆盖其他正在维护的代码。"""
    if new in text:
        return text
    if text.count(old) != 1:
        raise RuntimeError('上下文补丁锚点不唯一或已变化')
    return text.replace(old,new,1)

def patched_sources(root=ROOT):
    result = {}
    path = root/'src/maisaka/chat_loop_service.py'
    text = path.read_text()
    text = replace_once(text,
        '''        final_user_messages = [
            *(injected_user_messages or []),
            self._build_current_time_user_message(),
            *(tail_user_messages or []),
            current_chat_attention,
        ]''',
        '''        # QQbot缓存：外部资料保持user；仅宿主生成的时间、规则放在稳定历史之后。
        final_user_messages = [
            *(injected_user_messages or []),
            *(tail_user_messages or []),
        ]''')
    text = replace_once(text,
        '''        normalized_final_user_message = str(final_user_message or "").strip()
        if normalized_final_user_message:
            items.append(
                ContextItemBuilder()
                .set_role(RoleType.User)
                .add_text_content(normalized_final_user_message)
                .build()
            )

        return items, history_item_count''',
        '''        # 宿主即时上下文不写入聊天历史，也不把网页、记忆引文提升成指令。
        for host_message in (self._build_current_time_user_message(), current_chat_attention, final_user_message):
            normalized_host_message = str(host_message or "").strip()
            if normalized_host_message:
                items.append(ContextItemBuilder().set_role(RoleType.System)
                             .add_text_content("【宿主本轮上下文】\\n" + normalized_host_message).build())

        return items, history_item_count''')
    result[path] = text
    path = root/'src/chat/replyer/maisaka_generator_base.py'
    text = path.read_text()
    text = replace_once(text,
        '''        current_time = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        sections: List[str] = [f"当前时间：{current_time}"]''',
        '''        # QQbot缓存：目标消息和其他引文仍是user，易变时间另放宿主后缀。
        sections: List[str] = []''')
    text = replace_once(text,
        '''        if enable_visual_message:
            return limit_latest_images_in_messages(
                items,
                max_image_num=global_config.visual.max_image_num,
            )
        return items''',
        '''        # 每轮使用新时间，不复用旧状态；仅这条宿主数据采用system身份。
        current_time = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        items.append(ContextItemBuilder().set_role(RoleType.System)
                     .add_text_content(f"【宿主本轮上下文】\\n当前时间：{current_time}").build())
        if enable_visual_message:
            return limit_latest_images_in_messages(
                items,
                max_image_num=global_config.visual.max_image_num,
            )
        return items''')
    result[path] = text
    path = root/'src/llm_models/model_client/openai_client.py'
    text = path.read_text()
    anchor = '''            openai_response_format = _convert_response_format(request.response_format)'''
    replacement = '''            # QQbot缓存：只对会话planner/replyer稳定已知宿主规则和工具顺序。
            if request.trace_context and request.trace_context.task_name in {'planner', 'replyer'}:
                from .chatgpt_plan_cache_layout import stable_instruction_prefix, stable_tool_definitions
                messages_payload = stable_instruction_prefix(messages_payload)
                if tools_payload:
                    tools_payload = stable_tool_definitions(tools_payload)
            openai_response_format = _convert_response_format(request.response_format)'''
    result[path] = replace_once(text,anchor,replacement)
    result[root/'src/llm_models/model_client/chatgpt_plan_cache_layout.py'] = (SOURCE/'chatgpt-plan/cache_layout.py').read_text()
    for path,text in result.items():
        compile(text,str(path),'exec')
    return result

def apply():
    changes = patched_sources()
    stamp = datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')
    folder = ROOT/'data/context-cache'/stamp
    for path,text in changes.items():
        if path.read_text() == text:
            continue
        backup = folder/path.relative_to(ROOT)
        backup.parent.mkdir(parents=True,exist_ok=True)
        shutil.copy2(path,backup)
        path.write_text(text)
    print(json.dumps({'source_files':len(changes),'backup':str(folder),'services_restarted':False}))

def activate():
    import tomlkit
    verified = json.loads((SOURCE/'data/deepseek-layout-benchmark/result.json').read_text())
    if len(verified['requests']) != 12 or not all(r.get('success') and r.get('has_text') for r in verified['requests']):
        raise RuntimeError('同模型对照未全部通过，不能切换生产模型')
    path = ROOT/'config/model_config.toml'
    text = path.read_text()
    doc = tomlkit.parse(text)
    stamp = datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')
    folder = ROOT/'data/context-cache'/stamp
    folder.mkdir(parents=True,exist_ok=True)
    backup = folder/'model-config-before.toml'
    backup.write_text(text)
    backup.chmod(0o600)
    for name,source_name in [('deepseek-v4-flash','deepseek-flash'),('deepseek-v4-flash-thinking','deepseek-flash-thinking')]:
        if not any(m['name'] == name for m in doc['models']):
            data = deepcopy(next(m for m in doc['models'] if m['name'] == source_name).unwrap())
            data.update(name=name,model_identifier='deepseek-v4-flash')
            doc['models'].append(data)
    doc['model_task_config']['planner']['model_list'] = ['deepseek-v4-flash-thinking']
    doc['model_task_config']['replyer']['model_list'] = ['deepseek-v4-flash']
    sys.path.insert(0,str(ROOT))
    from src.config.config import ModelConfig
    try:
        ModelConfig(**{k:v for k,v in doc.unwrap().items() if k != 'inner'})
    except Exception:
        raise RuntimeError('模型配置验证失败，未写入；为避免凭据泄露不输出配置对象') from None
    path.write_text(tomlkit.dumps(doc))
    with sqlite3.connect('file:'+str(ROOT/'data/MaiBot.db')+'?mode=ro',uri=True) as db:
        last_id = db.execute('SELECT COALESCE(MAX(id),0) FROM llm_usage').fetchone()[0]
    report = {'activated_at':datetime.now(timezone.utc).isoformat(),'baseline_usage_id':last_id,
              'planner':'deepseek-v4-flash-thinking','replyer':'deepseek-v4-flash',
              'backup':str(backup),'source_sha256':{str(p.relative_to(ROOT)):hashlib.sha256(p.read_bytes()).hexdigest() for p in patched_sources()},
              'services_restarted':False}
    (ROOT/'data/context-cache/deployment.json').write_text(json.dumps(report,ensure_ascii=False,indent=2))
    print(json.dumps(report,ensure_ascii=False))

if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('--apply',action='store_true')
    parser.add_argument('--activate',action='store_true')
    args = parser.parse_args()
    if args.apply:
        apply()
    elif args.activate:
        activate()
    else:
        patched_sources()
        print('补丁锚点和语法验证通过，未写入生产文件')

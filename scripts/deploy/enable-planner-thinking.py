"""只启用规划器思考，补连续工具协议；保留聊天、模型通道和既有配置。"""
import ast
import copy
from datetime import datetime, timezone
from pathlib import Path
import shutil
import os
import tomllib

import tomlkit

ROOT = Path('/home/qqbot/QQbot/MaiBot')
SOURCE = Path('/mnt/d/code/web/QQbot')


def replace_checked(path, old, new):
    """幂等修改确定代码块，遇到上游变更停止，不覆盖无关代码。"""
    text = path.read_text()
    if new in text:
        return
    if text.count(old) != 1:
        raise RuntimeError('兼容修改定位失败：' + path.name)
    updated = text.replace(old, new, 1)
    ast.parse(updated)
    stamp = datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')
    shutil.copy2(path, path.with_name(path.name + '.before-thinking-' + stamp))
    path.write_text(updated)


def install_compat(root=ROOT):
    """仅官方DeepSeek开启思考时用新投影，其他模型沿用原路径。"""
    shutil.copy2(SOURCE / 'deepseek_thinking_compat.py', root / 'deepseek_thinking_compat.py')
    client = root / 'src/llm_models/model_client/openai_client.py'
    old = '            messages_payload: List[ChatCompletionMessageParam] = _convert_messages(request_messages)\n'
    new = old + ("            # DeepSeek连续工具调用需要原样回传原生推理，仅思考请求启用。\n"
                 "            if urlparse(self.api_provider.base_url).hostname == 'api.deepseek.com' and request.extra_params.get('thinking', {}).get('type') == 'enabled':\n"
                 "                from deepseek_thinking_compat import convert_thinking_messages\n"
                 "                messages_payload = convert_thinking_messages(request_messages, _convert_messages)\n")
    replace_checked(client, old, new)
    tone = root / 'plugins/qqbot-task-assist/tone_context.py'
    # 语气过滤已移除时不重建；仅兼容尚未升级的旧部署。
    if tone.exists():
        replace_checked(tone, "if item.get('item_type') not in {'AssistantMessageItem', 'ReasoningItem'}]",
                        "if item.get('item_type') != 'AssistantMessageItem']")


def enable():
    """复制一个思考模型别名，只替换planner的选择；其他任务逐项校验不变。"""
    install_compat()
    path = ROOT / 'config/model_config.toml'
    before = path.read_text()
    original = tomllib.loads(before)
    document = tomlkit.parse(before)
    model_name = 'deepseek-flash-thinking'
    base = next(m for m in original['models'] if m['name'] == 'deepseek-flash')
    thinking = copy.deepcopy(base)
    thinking.update(name=model_name, send_temperature=False)
    thinking['extra_params'] = {**thinking.get('extra_params', {}),
                               'thinking': {'type': 'enabled'}, 'reasoning_effort': 'low'}
    expected = copy.deepcopy(original)
    existing = next((m for m in expected['models'] if m['name'] == model_name), None)
    if existing is not None:
        if existing != thinking:
            raise RuntimeError('同名思考模型已有不同设置，停止覆盖')
    else:
        expected['models'].append(thinking)
        document['models'].append(thinking)
    expected['model_task_config']['planner']['model_list'] = [model_name]
    document['model_task_config']['planner']['model_list'] = [model_name]
    after = tomlkit.dumps(document)
    assert tomllib.loads(after) == expected, '发生规划器之外的模型配置修改'
    if after != before:
        stamp = datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')
        shutil.copy2(path, path.with_name(path.name + '.before-thinking-' + stamp))
        temporary = path.with_name(path.name + '.thinking.tmp')
        with os.fdopen(os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600), 'w') as output:
            output.write(after)
        temporary.chmod(0o600)
        temporary.replace(path)
    print('规划器已设为DeepSeek思考low；日常回复、记忆、视觉及网络配置未变，尚需重载。')


if __name__ == '__main__':
    enable()

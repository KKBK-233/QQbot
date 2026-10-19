"""幂等部署官方 ChatGPT provider；通过验证后再切换对话模型。"""

from pathlib import Path

import argparse
from datetime import datetime, timezone
import json
import os
import py_compile
import sys
import time

import tomlkit

ROOT = Path('/home/qqbot/QQbot/MaiBot')
SOURCE = Path('/mnt/d/code/web/QQbot/chatgpt-plan')


def install() -> None:
    login_root = ROOT.parent / 'chatgpt-plan'
    login_root.mkdir(exist_ok=True)
    for name in ['auth.py', 'login_server.py']:
        payload = (SOURCE / name).read_bytes()
        compile(payload, name, 'exec')
        (login_root / name).write_bytes(payload)
    target = ROOT / 'src/llm_models/model_client'
    for source, name in [('auth.py', 'chatgpt_plan_auth.py'), ('cache_layout.py', 'chatgpt_plan_cache_layout.py'),
                         ('openai_chatgpt_plan.py', 'openai_chatgpt_plan.py')]:
        payload = (SOURCE / source).read_bytes()
        compile(payload, name, 'exec')
        (target / name).write_bytes(payload)
        py_compile.compile(str(target / name), doraise=True)
    registry = target / '__init__.py'
    text = registry.read_text()
    line = '    "openai_chatgpt_plan": ".openai_chatgpt_plan",\n'
    if line not in text:
        anchor = '    "openai_responses": ".openai_responses_client",\n'
        if text.count(anchor) != 1:
            raise RuntimeError('provider 注册锚点发生变化')
        registry.write_text(text.replace(anchor, anchor + line))
    # 只补充此登录服务，不覆盖生产中其他 Supervisor 程序的配置。
    supervisor = ROOT.parent / 'supervisord.conf'
    config_text = supervisor.read_text()
    marker = '[program:chatgpt-plan-login]'
    if marker not in config_text:
        source_config = (SOURCE.parent / 'supervisord.conf').read_text()
        if source_config.count(marker) != 1:
            raise RuntimeError('登录服务 Supervisor 配置缺失')
        supervisor.write_text(config_text + '\n' + marker + source_config.split(marker)[1])


def activate(restore: bool = False, effort='low', with_vision=False, model_id='gpt-6.1-sol', replace_haiku=False) -> None:
    config = ROOT / 'config/model_config.toml'
    backup = ROOT / 'data/chatgpt-plan/model-config-before.toml'
    switch_manifest = backup.parent / 'haiku-switch-latest.json'
    if restore:
        if replace_haiku:
            restore_haiku(config, switch_manifest)
            return
        if with_vision:
            checkpoints = sorted(backup.parent.glob('model-config-before-*.toml'))
            if not checkpoints:
                raise RuntimeError('尚无三项任务切换前配置')
            backup = checkpoints[-1]
        if not backup.exists():
            raise RuntimeError('尚无切换前配置')
        # 只恢复此接入修改的字段，保留之后用户对其他配置所作的修改。
        current = tomlkit.parse(config.read_text())
        previous = tomlkit.parse(backup.read_text())
        for task in (['planner', 'replyer', 'vlm'] if with_vision else ['planner', 'replyer']):
            current['model_task_config'][task]['model_list'] = previous['model_task_config'][task]['model_list']
        config.write_text(tomlkit.dumps(current))
        print('已恢复所选任务原模型，重启 MaiBot 后生效')
        return
    sys.path.insert(0, str(ROOT))
    from src.llm_models.model_client.chatgpt_plan_auth import AccountStore
    if not AccountStore().public_status()['authenticated']:
        raise RuntimeError('请先登录个人账号')
    report = ROOT / 'data/chatgpt-plan/verification.json'
    if not report.exists():
        raise RuntimeError('尚未验证真实文本与工具调用')
    verified = json.loads(report.read_text())
    if not verified.get('success') or verified.get('model') != model_id:
        raise RuntimeError('模型验证未通过')
    if verified.get('reasoning_effort') != effort or ((with_vision or replace_haiku) and not verified.get('vision_verified')):
        raise RuntimeError('所选思考级别或视觉尚未验证')
    if time.time() - verified.get('verified_at', 0) > 3600:
        raise RuntimeError('请在切换前重新验证当前账号')
    # 切换时核对验证账号，防止验证 A 账号后误启用 B 账号。
    with AccountStore().locked() as account_data:
        if verified.get('account_key') != account_data['active']:
            raise RuntimeError('当前账号发生变化，请重新验证')
    text = config.read_text()
    # 每次切换留存完整配置；凭据仅保存在受保护的运行目录中。
    checkpoint = backup.parent / ('model-config-before-' + datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%S%fZ') + '.toml')
    checkpoint.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    checkpoint.write_text(text)
    checkpoint.chmod(0o600)
    if not backup.exists():
        backup.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        backup.write_text(text)
        backup.chmod(0o600)
    doc = tomlkit.parse(text)
    if not any(p['name'] == 'ChatGPTPlan' for p in doc['api_providers']):
        doc['api_providers'].append({'name': 'ChatGPTPlan', 'base_url': 'https://api.openai.com/v1',
                                    'client_type': 'openai_chatgpt_plan', 'auth_type': 'none',
                                    'max_retry': 0, 'timeout': 180, 'retry_interval': 4})
    selected = model_id.replace('gpt-', 'chatgpt-', 1) + '-' + effort
    if not any(m['name'] == selected for m in doc['models']):
        doc['models'].append({'name': selected, 'model_identifier': model_id, 'api_provider': 'ChatGPTPlan',
                              'visual': True, 'force_stream_mode': True, 'send_temperature': False,
                              'extra_params': {'reasoning': {'effort': effort}}})
    chosen = next(m for m in doc['models'] if m['name'] == selected)
    if chosen['model_identifier'] != model_id or chosen['api_provider'] != 'ChatGPTPlan' or chosen.get('extra_params', {}).get('reasoning', {}).get('effort') != effort:
        raise RuntimeError('已有模型别名配置与验证不一致')
    if replace_haiku:
        previous_lists = replace_haiku_tasks(doc, selected)
        tasks = list(previous_lists)
        if not tasks:
            raise RuntimeError('当前没有使用 Haiku 的任务，未修改配置')
    else:
        tasks = ['planner', 'replyer', 'vlm'] if with_vision else ['planner', 'replyer']
        previous_lists = {task: list(doc['model_task_config'][task]['model_list']) for task in tasks}
        for task in tasks:
            doc['model_task_config'][task]['model_list'] = [selected]
    os.chdir(ROOT)
    from src.config.config import ModelConfig
    ModelConfig(**{key: value for key, value in doc.unwrap().items() if key != 'inner'})
    config.write_text(tomlkit.dumps(doc))
    if replace_haiku:
        switch_manifest.write_text(json.dumps({'checkpoint': checkpoint.name, 'model': model_id,
                                              'effort': effort, 'previous_lists': previous_lists},
                                             ensure_ascii=False, indent=2))
        switch_manifest.chmod(0o600)
    print('/'.join(tasks) + ' 已切换到 ' + model_id + ' ' + effort + '，重启 MaiBot 后生效')


def replace_haiku_tasks(doc, selected):
    """仅替换实际任务列表中的 Haiku，保留其他候选模型及其顺序。"""
    haiku = {model['name'] for model in doc['models']
             if 'haiku' in (model['name'] + ' ' + model['model_identifier']).lower()}
    previous_lists = {}
    for name, task in doc['model_task_config'].items():
        old = list(task['model_list'])
        if not any(model in haiku for model in old):
            continue
        previous_lists[name] = old
        task['model_list'] = list(dict.fromkeys(selected if model in haiku else model for model in old))
    return previous_lists


def restore_haiku(config, manifest):
    """按本轮清单恢复受影响任务，不覆盖其余任务及后续配置。"""
    if not manifest.exists():
        raise RuntimeError('尚无全部 Haiku 切换记录')
    data = json.loads(manifest.read_text())
    current = tomlkit.parse(config.read_text())
    for task, previous in data['previous_lists'].items():
        current['model_task_config'][task]['model_list'] = previous
    config.write_text(tomlkit.dumps(current))
    print('已恢复本轮替换的 Haiku 任务，重启 MaiBot 后生效')


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('--activate', action='store_true')
    parser.add_argument('--restore', action='store_true')
    parser.add_argument('--model', choices=['gpt-6.1-sol', 'gpt-6-luna'], default='gpt-6.1-sol')
    parser.add_argument('--replace-haiku', action='store_true', help='替换全部正在使用 Haiku 的任务；配合 --restore 回退本轮替换')
    parser.add_argument('--effort', choices=['low', 'medium', 'high'], default='low')
    parser.add_argument('--with-vision', action='store_true')
    args = parser.parse_args()
    install()
    if args.activate or args.restore:
        activate(args.restore, args.effort, args.with_vision, args.model, args.replace_haiku)
    else:
        print('provider 已安装；当前运行模型未切换')

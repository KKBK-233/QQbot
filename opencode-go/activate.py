"""切换主会话或全部已启用任务模型，保留任务参数和私有备份。"""
import argparse
from copy import deepcopy
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import sqlite3
import sys

import tomlkit

ROOT = Path('/home/qqbot/QQbot/MaiBot')
MODEL = 'opencode-go-haiku-5.5'
LOW_MODEL = MODEL + '-low'
EFFORT_MODELS = {'low': LOW_MODEL, 'medium': MODEL + '-medium'}


def private_write(path, text):
    """原子保存配置；备份包含其他供应商凭据，只能在私有目录留存。"""
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    temp = path.with_name(path.name + '.tmp')
    fd = os.open(temp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, 'w') as handle:
        handle.write(text)
    temp.chmod(0o600)
    temp.replace(path)


def task_routes(config, selected, rollback=False, all_tasks=False):
    """全量切换覆盖已启用的生成任务，不为未启用任务虚构模型能力。"""
    if rollback:
        routes = {'planner': 'deepseek-v4-flash-thinking', 'replyer': 'deepseek-v4-flash'}
        if all_tasks:
            # 恢复切换 Haiku 前的七类辅助任务；空任务和未知任务保持原路由。
            for name in ('memory', 'mid_memory', 'utils', 'learner', 'expression_use', 'emoji', 'vlm'):
                if config['model_task_config'].get(name, {}).get('model_list'):
                    routes[name] = 'deepseek-flash'
        return routes
    if not all_tasks:
        return {'planner': selected, 'replyer': selected}
    unsupported = {'voice', 'embedding', 'image_embedding'}
    enabled = {name for name, task in config['model_task_config'].items() if task.get('model_list')}
    if enabled & unsupported:
        raise ValueError('Haiku不能接管已启用的语音或嵌入任务')
    return {name: selected for name in config['model_task_config'] if name in enabled}


def activate(rollback=False, effort=None, all_tasks=False):
    """更新选定任务的模型路由，完整校验后原子写入并留存回退备份。"""
    os.chdir(ROOT)
    sys.path.insert(0, str(ROOT))
    from src.config.config import ModelConfig

    path = ROOT / 'config/model_config.toml'
    before = path.read_text()
    doc = tomlkit.parse(before)
    expected = deepcopy(doc.unwrap())
    selected = EFFORT_MODELS[effort] if effort else MODEL
    if effort:
        # 独立模型别名保存真实参数，原关闭思考别名继续可用于对照和回退。
        params = {'thinking': {'type': 'adaptive', 'display': 'omitted'}, 'output_config': {'effort': effort}}
        model = next((m for m in expected['models'] if m['name'] == selected), None)
        if model is None:
            model = deepcopy(next(m for m in expected['models'] if m['name'] == MODEL))
            model.update(name=selected, extra_params=params)
            expected['models'].append(model)
            doc['models'].append(model)
        elif model.get('extra_params') != params:
            raise RuntimeError('已有' + effort + '别名的参数不同，停止覆盖')
    tasks = task_routes(expected, selected, rollback, all_tasks)
    if not set(tasks.values()).issubset({m['name'] for m in doc['models']}):
        raise RuntimeError('目标模型尚未安装')
    if not rollback:
        key = Path.home() / '.config/maibot-opencode-go/api-key'
        if not key.exists() or key.stat().st_mode & 0o077:
            raise RuntimeError('凭据不存在或权限不正确')
    for task, model in tasks.items():
        doc['model_task_config'][task]['model_list'] = [model]
        expected['model_task_config'][task]['model_list'] = [model]
    planner_limit = 16384 if rollback else 8192
    doc['model_task_config']['planner']['max_tokens'] = planner_limit
    expected['model_task_config']['planner']['max_tokens'] = planner_limit
    if doc.unwrap() != expected:
        raise RuntimeError('出现无关配置修改')
    try:
        ModelConfig(**{k: v for k, v in expected.items() if k != 'inner'})
    except Exception:
        raise RuntimeError('模型配置验证失败，未写入') from None
    stamp = datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')
    backup = ROOT / ('data/opencode-go/model-config-before-switch-' + stamp + '.toml')
    after = tomlkit.dumps(doc)
    if after != before:
        private_write(backup, before)
        private_write(path, after)
    with sqlite3.connect('file:' + str(ROOT / 'data/MaiBot.db') + '?mode=ro', uri=True) as db:
        baseline = db.execute('SELECT COALESCE(MAX(id),0) FROM llm_usage').fetchone()[0]
    report = {'activated_at': datetime.now(timezone.utc).isoformat(), 'tasks': tasks,
              'planner_max_tokens': planner_limit, 'baseline_usage_id': baseline,
              'thinking': 'adaptive' if effort else None, 'effort': effort, 'all_tasks': all_tasks,
              'config_changed': before != after, 'services_restarted': False}
    private_write(ROOT / 'data/opencode-go/activation.json', json.dumps(report, ensure_ascii=False, indent=2))
    return report


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('--rollback-deepseek', action='store_true')
    parser.add_argument('--low', action='store_true')
    parser.add_argument('--medium', action='store_true')
    parser.add_argument('--all-tasks', action='store_true', help='切换全部已启用的生成任务，保留未启用任务')
    args = parser.parse_args()
    if args.low and args.medium:
        parser.error('low与medium只能选一个')
    effort = 'low' if args.low else 'medium' if args.medium else None
    if effort and args.rollback_deepseek:
        parser.error('思考档位与DeepSeek回退不能同时使用')
    print(json.dumps(activate(args.rollback_deepseek, effort, args.all_tasks), ensure_ascii=False))

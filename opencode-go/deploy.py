"""安装Haiku套餐可选客户端；只添加模型，不切换任何生产任务。"""
import ast
from datetime import datetime, timezone
import os
from pathlib import Path
import sys
import tomllib

import tomlkit

ROOT = Path('/home/qqbot/QQbot/MaiBot')
SOURCE = Path(__file__).resolve().parent
MODEL_NAME = 'opencode-go-haiku-5.5'


def private_write(path, content):
    """原子写入私有配置和备份，不把现有API凭据写进仓库。"""
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    temporary = path.with_name(path.name + '.tmp')
    fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, 'w') as handle:
        handle.write(content)
    temporary.chmod(0o600)
    temporary.replace(path)


def install():
    """校验源码和完整配置，只新增独立provider与模型别名。"""
    target = ROOT / 'src/llm_models/model_client'
    files = {'client.py': 'opencode_go_client.py', 'protocol.py': 'opencode_go_protocol.py',
             'cache_layout.py': 'opencode_go_cache_layout.py', 'native.py': 'opencode_go_native.py'}
    for source, name in files.items():
        content = (SOURCE / source).read_text(encoding='utf-8-sig')
        ast.parse(content)
        private_write(target / name, content)
    registry = target / '__init__.py'
    text = registry.read_text()
    line = '    "opencode_go": ".opencode_go_client",\n'
    if line not in text:
        anchor = '    "openai_responses": ".openai_responses_client",\n'
        if text.count(anchor) != 1:
            raise RuntimeError('客户端注册锚点发生变化')
        updated = text.replace(anchor, anchor + line)
        ast.parse(updated)
        private_write(registry, updated)
    path = ROOT / 'config/model_config.toml'
    before = path.read_text()
    original = tomllib.loads(before)
    doc = tomlkit.parse(before)
    provider = {'name': 'OpenCodeGo', 'base_url': 'https://opencode.ai/zen/go/v1', 'client_type': 'opencode_go',
                'auth_type': 'none', 'max_retry': 0, 'timeout': 90, 'retry_interval': 1}
    model = {'name': MODEL_NAME, 'model_identifier': 'claude-haiku-5-5', 'api_provider': 'OpenCodeGo',
             'visual': True, 'send_temperature': False, 'force_stream_mode': False, 'max_tokens': 8192}
    for section, value in [('api_providers', provider), ('models', model)]:
        existing = next((item for item in original[section] if item['name'] == value['name']), None)
        if existing is not None and existing != value:
            raise RuntimeError('同名配置已存在不同设置，停止覆盖')
        if existing is None:
            doc[section].append(value)
    after = tomlkit.dumps(doc)
    expected = {**original, 'api_providers': original['api_providers'] + ([] if provider in original['api_providers'] else [provider]),
                'models': original['models'] + ([] if model in original['models'] else [model])}
    if tomllib.loads(after) != expected:
        raise RuntimeError('出现无关配置修改')
    os.chdir(ROOT)
    sys.path.insert(0, str(ROOT))
    from src.config.config import ModelConfig
    ModelConfig(**{key: value for key, value in doc.unwrap().items() if key != 'inner'})
    if after != before:
        stamp = datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')
        private_write(ROOT / ('data/opencode-go/model-config-before-' + stamp + '.toml'), before)
        private_write(path, after)
    print('OpenCode Go Haiku 5.5已添加为可选模型；所有生产任务选择不变，密钥不进入模型配置。')


if __name__ == '__main__':
    install()

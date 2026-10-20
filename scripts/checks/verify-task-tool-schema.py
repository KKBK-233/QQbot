"""执行 SDK 工具声明到宿主工具发现的真实转换，不调用模型或发送群消息。"""

from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import argparse
import asyncio
import importlib.util
import io
import json
import os
import sys

ROOT = Path('/home/qqbot/QQbot/MaiBot')


async def verify(source):
    """声明、宿主 schema、Provider 和 Registry 都执行正式生产方法。"""
    os.chdir(ROOT)
    sys.path.insert(0, str(ROOT))
    spec = importlib.util.spec_from_file_location('task_schema_probe', source / 'plugin.py',
                                                submodule_search_locations=[str(source)])
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    from src.core.tooling import ToolAvailabilityContext, ToolRegistry
    from src.plugin_runtime.component_query import ComponentQueryService, component_query_service
    from src.plugin_runtime.tool_provider import PluginToolProvider

    components = module.create_plugin().get_components()
    specs = {}
    for component in components:
        if component['type'] != 'TOOL':
            continue
        data = component['metadata']
        entry = SimpleNamespace(name=component['name'], description=data['description'],
            parameters=data['parameters'], parameters_raw=data['parameters_raw'], metadata=data,
            plugin_id='qqbot-task-assist', invoke_method=data['invoke_method'], legacy_component_type='tool')
        specs[entry.name] = ComponentQueryService._build_tool_spec(entry)
    schema = specs['analyze_task_image'].parameters_schema
    assert schema['properties']['image_numbers']['items']['type'] == 'integer'
    assert 'image_numbers' not in schema['required']
    registry = ToolRegistry()
    registry.register_provider(PluginToolProvider())
    with patch.object(component_query_service, 'get_llm_available_tool_specs', return_value=specs):
        discovered = await registry.list_tools(ToolAvailabilityContext(platform='qq', is_group_chat=True,
            group_id='1022382380', session_id='schema-probe', user_id='1020050112'))
    for item in discovered:
        item.to_llm_definition()
    assert any(item.name == 'analyze_task_image' for item in discovered)
    return {'sdk_declaration_checked': True, 'host_schema_checked': True, 'registry_discovery_checked': True,
            'image_numbers_item_type': schema['properties']['image_numbers']['items']['type'],
            'model_calls': 0, 'qq_sends': 0}


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('--source', type=Path, default=ROOT / 'plugins/qqbot-task-assist')
    args = parser.parse_args()
    with redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()):
        result = asyncio.run(verify(args.source))
    print(json.dumps(result, ensure_ascii=False))

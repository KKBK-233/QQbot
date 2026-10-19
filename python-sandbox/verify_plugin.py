"""通过真实SDK检验插件生命周期、注册及调用，不调用模型或发送QQ消息。"""

import asyncio
import importlib.util
import json
from pathlib import Path
import sys
import tempfile


async def verify():
    """先实例化，再运行一次实际文件处理，避免仅测底层运行器。"""
    source = Path(__file__).resolve().parent
    spec = importlib.util.spec_from_file_location('sandbox_plugin_probe', source / 'plugin.py',
                                               submodule_search_locations=[str(source)])
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    plugin = module.create_plugin()
    await plugin.on_load()
    temporary = tempfile.TemporaryDirectory()
    plugin.budget = module.Budget(temporary.name)
    try:
        components = plugin.get_components()
        assert len(components) == 1, components
        result = await plugin.execute("from pathlib import Path; print(sum([2, 4, 6])); Path('output/answer.txt').write_text('12')",
                                      stream_id='admin-probe', user_id='admin-probe', _sandbox_trusted_context=True)
        assert result['success'] and result['stdout'] == '12\n' and result['files'] == {'answer.txt': '12'}, result
        # 立即重试必须在执行前被拒绝，不产生第二次沙箱用量。
        rejected = await plugin.execute('print(999)', stream_id='admin-probe', user_id='admin-probe',
                                        _sandbox_trusted_context=True)
        assert not rejected['success'] and 'usage' not in rejected, rejected
        untrusted = await plugin.execute('print(999)', stream_id='fake', _sandbox_trusted_context=False)
        assert not untrusted['success'] and 'usage' not in untrusted, untrusted
        await plugin.on_config_update('global', {}, 'test')
    finally:
        await plugin.on_unload()
        temporary.cleanup()
    print(json.dumps({'sdk_registration': True, 'tool_execution': True, 'budget_rejection': True,
                      'usage': result['usage'], 'qq_messages_sent': 0}))


if __name__ == '__main__':
    asyncio.run(verify())

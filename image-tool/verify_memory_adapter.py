"""真实SDK回放记住纠正、重启后自动应用原图、查看及撤销，不调用真实生图后端。"""
import asyncio
import base64
import importlib.util
import json
from pathlib import Path
import sys
import tempfile
from types import SimpleNamespace
from unittest.mock import AsyncMock

folder = Path(__file__).resolve().parent / 'maibot-plugin'
spec = importlib.util.spec_from_file_location('memory_adapter', folder / 'plugin.py', submodule_search_locations=[str(folder)])
module = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = module
spec.loader.exec_module(module)


async def main():
    """用隔离群编号和临时记忆目录，后端与QQ发送均为替身。"""
    session = 'memory-verification-group'
    quote = '尾柄从后腰伸出，尾鳍只有左右两片'
    requests = {
        'correction': {'session_id': session, 'message_info': {'user_info': {'user_id': '123'}},
                       'raw_message': [{'type': 'text', 'data': '记住：' + quote}]},
        'chosen-reference': {'session_id': session, 'message_info': {'user_info': {'user_id': '3976405558'}},
                             'raw_message': [{'type': 'image'}]},
        'undo-request': {'session_id': session, 'message_info': {'user_info': {'user_id': '123'}},
                         'raw_message': [{'type': 'text', 'data': '撤销Q版角色记忆最近一次更新'}]}
    }
    async def message(mid, **kwargs):
        return requests.get(mid)

    paths = []
    async def reference(session, mid, rid):
        path = Path('/mnt/d/code/web/QQbot/data/image-tool/inbox', rid + '.png')
        path.write_bytes(base64.b64decode('iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+aV1cAAAAASUVORK5CYII='))
        paths.append(path)
        return path.name

    with tempfile.TemporaryDirectory() as root:
        def new_plugin():
            plugin = module.create_plugin()
            plugin._ctx = SimpleNamespace(message=SimpleNamespace(get_by_id=message))
            plugin.memories = module.CharacterMemory(Path(root) / 'character_memory')
            plugin.tasks = {}
            plugin.deliver = AsyncMock()
            plugin.client = SimpleNamespace(call=AsyncMock(return_value={'content': [
                {'type': 'text', 'text': json.dumps({'state': 'queued'})}]}))
            plugin.import_reference = AsyncMock(side_effect=reference)
            return plugin

        plugin = new_plugin()
        saved = await plugin.image('remember', 'remember-tail', quote, form='chibi', memory_topic='tail',
            message_id='chosen-reference', instruction_message_id='correction', stream_id=session)
        assert saved['success'] and saved['memory']['revision'] == 1, saved
        plugin.client.call.assert_not_awaited()
        restarted = new_plugin()
        restarted.import_reference = AsyncMock(side_effect=AssertionError('不应再次依赖历史图片读取'))
        generated = await restarted.image('generate', 'apply-memory', 'DS娘Q版微笑', stream_id=session)
        assert generated['success'], generated
        args = restarted.client.call.await_args.args[0]
        assert args['action'] == 'edit' and args['references'] == [paths[0].name]
        assert quote in args['prompt'] and '本群已确认的动态修正' in args['prompt']
        await asyncio.gather(*restarted.tasks.values())
        viewed = await restarted.image('recall', 'view-memory', form='chibi', stream_id=session)
        assert viewed['memory']['facts']['tail']['source_message_id'] == 'correction'
        # 记忆不可跨群；模型也不能把自己编的描述替换成用户原话。
        rejected = await plugin.image('remember', 'reject-other-group', quote, form='chibi',
            instruction_message_id='correction', stream_id='another-group')
        assert not rejected['success']
        rejected = await plugin.image('remember', 'reject-invention', '紫色短发', form='chibi',
            instruction_message_id='correction', stream_id=session)
        assert not rejected['success']
        # 参考快照被替换时不能默默使用变过的图片。
        restarted.client.call.reset_mock()
        paths[0].write_bytes(b'changed')
        rejected = await restarted.image('generate', 'changed-reference', 'DS娘Q版微笑', stream_id=session)
        assert not rejected['success']
        restarted.client.call.assert_not_awaited()
        undone = await restarted.image('undo', 'undo-memory', form='chibi',
            instruction_message_id='undo-request', stream_id=session)
        assert undone['success'] and undone['memory']['facts'] == {}, undone
        restarted.client.call.assert_not_awaited()
    for path in paths:
        path.unlink(missing_ok=True)
    print('角色纠正持久化、重启后自动应用参考和特征、来源核对、群隔离、快照校验及撤销通过；真实生图与QQ发送均为0')


if __name__ == '__main__':
    asyncio.run(main())

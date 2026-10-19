"""在真实 MaiBot SDK 环境验证会话隔离、引用图片与一次性交付，不发送 QQ 消息。"""

import asyncio
import importlib.util
import json
from pathlib import Path
import tempfile
from types import SimpleNamespace
from unittest.mock import AsyncMock
import sys

folder = Path(__file__).resolve().parent / "maibot-plugin"
spec = importlib.util.spec_from_file_location("image_adapter", folder / "plugin.py", submodule_search_locations=[str(folder)])
module = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = module
spec.loader.exec_module(module)


async def main():
    assert module.scoped_id("group-a", "job") != module.scoped_id("group-b", "job")
    # 实际群调用的长编号必须可提交且查询稳定，不能截断成同一个任务。
    long_id = 'claude_edit_324281425_20261007_194000'
    normalized = module.scoped_id('group-a', long_id)
    assert len(normalized) <= 64
    assert normalized == module.scoped_id('group-a', long_id)
    assert normalized != module.scoped_id('group-a', long_id + '-retry')
    assert normalized != module.scoped_id('group-b', long_id)
    assert module.scoped_id('group-a', 'x' * 128)
    for value in ("../x", "", "x" * 129, None, '中文编号'):
        try:
            module.scoped_id("group-a", value)
            raise AssertionError("任务编号限制失效")
        except ValueError:
            pass
    with tempfile.TemporaryDirectory() as temporary:
        plugin = module.create_plugin()
        send = SimpleNamespace(image=AsyncMock(return_value=True), text=AsyncMock(return_value=True))
        plugin._ctx = SimpleNamespace(send=send, logger=SimpleNamespace(warning=lambda *args: None, info=lambda *args: None))
        plugin.tasks = {}
        plugin.delivery_dir = Path(temporary)
        plugin.memories = module.CharacterMemory(Path(temporary) / 'character_memory')
        # 只模拟成功后的交付，绝不发送到实际群聊。
        result = {"content": [{"type": "text", "text": json.dumps({"state": "succeeded"})},
                              {"type": "image", "data": "fixture"}]}
        plugin.client = SimpleNamespace(call=AsyncMock(return_value=result))
        await plugin.deliver("group-a", "job-a")
        await plugin.deliver("group-a", "job-a")
        assert send.image.await_count == 1
        assert send.image.await_args.args == ("fixture", "group-a")
        receipt = json.loads((plugin.delivery_dir / 'job-a.json').read_text())
        assert receipt['state'] == 'sent' and receipt['session'] == 'group-a'
        assert receipt['finished'] >= receipt['started'] and receipt['elapsed_seconds'] >= 0
        # 跨会话的引用消息在读取时丢弃。
        plugin._ctx.message = SimpleNamespace(get_by_id=AsyncMock(return_value={
            "session_id": "group-b", "raw_message": [{"type": "image", "binary_data_base64": "fixture"}]}))
        try:
            await plugin.import_reference("group-a", "message", "job")
            raise AssertionError("跨会话引用未被拒绝")
        except ValueError:
            pass
        # 只检查实际提交参数：DS娘文生图转为带角色参考的edit，不调用真实后端。
        session = 'eea9ec46d0880b86983de2e4235f4bca'
        plugin.import_reference = AsyncMock(return_value='confirmed-ds-reference.png')
        plugin.client.call = AsyncMock(return_value={'content': [{'type': 'text', 'text': json.dumps({'state': 'queued'})}]})
        plugin.deliver = AsyncMock()
        response = await plugin.image('generate', 'ds-check', 'DeepSeek娘Q版表情，深色短发猫耳长袍', stream_id=session)
        assert response['success'], response
        assert '任务编号：ds-check；状态：queued' in response['content'], '模型可见文字缺少实际状态'
        submitted = plugin.client.call.await_args.args[0]
        assert submitted['action'] == 'edit'
        assert submitted['references'] == ['confirmed-ds-reference.png']
        assert '渐变蓝色披散长卷发' in submitted['prompt']
        assert '【新画背景】' in submitted['prompt'] and '不透明背景' in submitted['prompt']
        assert '深色短发' not in submitted['prompt'] and '猫耳' not in submitted['prompt']
        assert plugin.import_reference.await_args.args[1] == '-1178010614'
        await asyncio.gather(*plugin.tasks.values())
        # 编辑实际原图时保留实景，仍要求完整不透明画布。
        await plugin.image('edit', 'ds-preserve-background', 'DS娘Q版只改表情',
                           message_id='user-original', stream_id=session)
        submitted = plugin.client.call.await_args.args[0]
        assert len(submitted['references']) == 2 and '【新画背景】' not in submitted['prompt']
        assert '【编辑背景】' in submitted['prompt'] and '不透明背景' in submitted['prompt']
        await asyncio.gather(*plugin.tasks.values())
        # 少女版同时实际附三视图与立绘，目标原图仍单独保留。
        plugin.import_reference = AsyncMock(side_effect=lambda session, mid, rid: {
            '-1461680975': 'girl-three-views.jpg', '-293459031': 'girl-portrait.jpg'
        }.get(mid, 'user-original.jpg'))
        await plugin.image('generate', 'ds-girl', 'DS娘少女版表情包', stream_id=session)
        submitted = plugin.client.call.await_args.args[0]
        assert submitted['references'] == ['girl-three-views.jpg', 'girl-portrait.jpg']
        assert '第1、2张附图' in submitted['prompt'] and '较长' in submitted['prompt']
        assert '2.5头身' not in submitted['prompt']
        await asyncio.gather(*plugin.tasks.values())
        await plugin.image('edit', 'ds-girl-original', 'DS娘少女版换掉这张', message_id='original', stream_id=session)
        submitted = plugin.client.call.await_args.args[0]
        assert submitted['references'] == ['girl-three-views.jpg', 'girl-portrait.jpg', 'user-original.jpg']
        assert '【新画背景】' not in submitted['prompt']
        await asyncio.gather(*plugin.tasks.values())
        await plugin.image('edit', 'ds-girl-four-inputs', 'DS娘少女版换掉这张',
                           message_id='original', parent_id='old-ds', stream_id=session)
        submitted = plugin.client.call.await_args.args[0]
        assert submitted['references'] == ['girl-three-views.jpg', 'user-original.jpg']
        assert submitted['parent_id'] == module.scoped_id(session, 'old-ds')
        assert '第2张附图' in submitted['prompt']
        await asyncio.gather(*plugin.tasks.values())
        # 普通对象的错误generate动作也不能丢掉用户明确指定的原图。
        plugin.import_reference = AsyncMock(return_value='user-original.png')
        response = await plugin.image('generate', 'ordinary-edit', '只把玩偶换成猫，其余不变',
                                      message_id='quoted-original', stream_id=session)
        assert response['success'], response
        submitted = plugin.client.call.await_args.args[0]
        assert submitted['action'] == 'edit' and submitted['references'] == ['user-original.png']
        await asyncio.gather(*plugin.tasks.values())
        await plugin.image('generate', 'ordinary-parent', '只把背景换成雨夜', parent_id='old-cat', stream_id=session)
        submitted = plugin.client.call.await_args.args[0]
        assert submitted['action'] == 'edit' and submitted['parent_id'] == module.scoped_id(session, 'old-cat')
        await asyncio.gather(*plugin.tasks.values())
        # 复现群里的实际调用：文字要求误填message_id，仍应读取固定角色图。
        async def reference_for_message(session, message_id, task_id):
            if message_id == '1754668179':
                raise module.MediaUnavailable('指定文字消息没有原图')
            return 'confirmed-ds-reference.png'
        plugin.import_reference = AsyncMock(side_effect=reference_for_message)
        plugin._ctx.message.get_by_id = AsyncMock(return_value={
            'session_id': session, 'raw_message': [{'type': 'text', 'data': '画DS娘Q版微笑'}]})
        response = await plugin.image('generate', 'ds-text-request', 'DS娘Q版微笑',
                                      message_id='1754668179', character='deepseek', stream_id=session)
        assert response['success'], response
        assert plugin.client.call.await_args.args[0]['references'] == ['confirmed-ds-reference.png']
        await asyncio.gather(*plugin.tasks.values())
        # 修改指定原图时不可吞掉缺图错误，也不能因此变成重画固定参考图。
        plugin.client.call.reset_mock()
        response = await plugin.image('edit', 'ds-missing-edit', 'DS娘换表情',
                                      message_id='1754668179', character='deepseek', stream_id=session)
        assert not response['success'] and not response['retryable']
        assert '指定文字消息没有原图' in response['content']
        plugin.client.call.assert_not_awaited()
        # 图片消息丢失原图时不能按纯文字降级，避免悄悄丢掉用户指定的构图。
        plugin._ctx.message.get_by_id = AsyncMock(return_value={
            'session_id': session, 'raw_message': [{'type': 'image'}]})
        response = await plugin.image('generate', 'ds-broken-reference', 'DS娘Q版微笑',
                                      message_id='1754668179', character='deepseek', stream_id=session)
        assert not response['success']
        plugin.client.call.assert_not_awaited()
        # 正在修改旧图时，父图排在第一张，角色参考的说明必须指向第二张。
        await plugin.image('edit', 'ds-parent', 'DS娘只换表情', parent_id='old-ds', character='deepseek', stream_id=session)
        assert '第2、3张附图' in plugin.client.call.await_args.args[0]['prompt']
        await asyncio.gather(*plugin.tasks.values())
        # 核实真人的外观覆盖，错误自述不能以同样参数覆盖默认注册卡。
        plugin._ctx.message.get_by_id = AsyncMock(return_value={
            'session_id': session, 'message_info': {'user_info': {'user_id': '123'}},
            'raw_message': [{'type': 'text', 'data': '把DS娘染成紫色短发'}]})
        response = await plugin.image('generate', 'ds-approved-dye', 'DS娘Q版微笑',
            appearance_change='染成紫色短发', instruction_message_id='human-request', stream_id=session)
        assert response['success'], response
        assert '【核实后的用户外观修改】染成紫色短发' in plugin.client.call.await_args.args[0]['prompt']
        await asyncio.gather(*plugin.tasks.values())
        plugin.client.call.reset_mock()
        response = await plugin.image('generate', 'ds-invented-dye', 'DS娘Q版微笑',
            appearance_change='变成黑发猫娘', instruction_message_id='human-request', stream_id=session)
        assert not response['success'], response
        plugin.client.call.assert_not_awaited()
        # 另一群可使用文字注册卡，但没有本群固定图，不读取其他群图片。
        plugin.import_reference.reset_mock()
        response = await plugin.image('generate', 'ds-other-group', '鲸鱼娘Q版微笑', stream_id='another-group')
        assert response['success'], response
        submitted = plugin.client.call.await_args.args[0]
        assert submitted['action'] == 'generate' and '本群未配置固定原图' in submitted['prompt']
        plugin.import_reference.assert_not_awaited()
        await asyncio.gather(*plugin.tasks.values())
        # 普通鱼不附加角色参考，也不改变generate动作。
        plugin.import_reference.reset_mock()
        await plugin.image('generate', 'fish-check', '蓝色大肥鱼', stream_id=session)
        assert plugin.client.call.await_args.args[0]['action'] == 'generate'
        plugin.import_reference.assert_not_awaited()
        await asyncio.gather(*plugin.tasks.values())
        # 历史失败和当前登录必须分别传给规划器，不能只截取旧错误文字。
        # 网上原图实际加入参数并进入edit；搜索动作本身不提交生图，跨群引用被拒绝。
        plugin.web_references = module.WebReferences(Path(temporary) / 'web_refs', Path(temporary) / 'web_inbox')
        cached = plugin.web_references.store(session, {'title': '公开角色图', 'url': 'https://example.com/source',
            'image': 'https://example.com/image.png'}, b'fixture-image-original', {'width': 800, 'height': 900})
        await plugin.image('generate', 'web-draw-check', '按参考角色在花园喝茶，采用新构图',
                           web_reference_id=cached['reference_id'], stream_id=session)
        submitted = plugin.client.call.await_args.args[0]
        assert submitted['action'] == 'edit' and len(submitted['references']) == 1
        assert '【网上参考用途】' in submitted['prompt'] and '以本次文字要求重新安排' in submitted['prompt']
        await asyncio.gather(*plugin.tasks.values())
        plugin.client.call.reset_mock()
        denied = await plugin.image('generate', 'web-other-group', '画参考角色',
                                    web_reference_id=cached['reference_id'], stream_id='other-group')
        assert not denied['success']
        plugin.client.call.assert_not_awaited()
        plugin.web_references.search = AsyncMock(return_value={'success': True, 'state': 'references_ready',
                                                               'references': [cached]})
        ready = await plugin.image('search_reference', 'search-only', '公开角色图 原作', stream_id=session)
        assert ready['state'] == 'references_ready'
        plugin.client.call.assert_not_awaited()
        plugin.client.call = AsyncMock(return_value={'content': [{'type': 'text', 'text': json.dumps({
            'state': 'failed', 'error': '缺少 Codex ChatGPT 登录授权，请先 codex login',
            'current_authorization': {'local_oauth_present': True, 'server_verified': False},
            'status_note': '这是历史失败。目前已发现凭据，用户明确重试时使用新的 request_id。'})}]})
        response = await plugin.image('status', 'old-auth-failure', stream_id=session)
        assert not response['success'] and response['current_authorization']['local_oauth_present']
        assert '历史失败' in response['content'] and '新的 request_id' in response['content']
        assert plugin.client.call.await_args.args[0]['action'] == 'status'
        assert module.scoped_id(session, 'old-auth-failure') not in plugin.tasks
        # 两个官方Hook独立传递能力与身份；不需要执行或重放任务。
        for hook in (plugin.planner_facts, plugin.reply_facts):
            prepared = await hook(session_id='no-real-history', items=[])
            text = prepared['modified_kwargs']['items'][-1]['parts'][0]['text']
            assert '当前已加载chatgpt_image' in text and '银灰色长发、紫色眼睛' in text
    print("真实 SDK 导入、群隔离、交付去重、DS娘及网上参考传递、历史失败与当前授权区分均通过；未生图或发送 QQ 消息")


if __name__ == "__main__":
    asyncio.run(main())

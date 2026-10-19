"""真实SDK下核对已注册角色参考、同框顺序和记忆隔离，模拟后端，不生图、不发QQ。"""

import asyncio
import hashlib
import importlib.util
import json
from pathlib import Path
import sys
import tempfile
from types import SimpleNamespace
from unittest.mock import AsyncMock


async def verify():
    folder = Path(__file__).resolve().parent / 'maibot-plugin'
    spec = importlib.util.spec_from_file_location('ai_character_adapter', folder / 'plugin.py',
                                                  submodule_search_locations=[str(folder)])
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    rules = sys.modules[spec.name + '.character_rules']
    checked = []
    with tempfile.TemporaryDirectory() as root:
        plugin = module.create_plugin()
        plugin._ctx = SimpleNamespace(message=SimpleNamespace(get_by_id=AsyncMock()))
        plugin.tasks = {}
        plugin.memories = module.CharacterMemory(root)
        plugin.import_reference = AsyncMock(return_value='user-original.png')
        plugin.deliver = AsyncMock()
        plugin.client = SimpleNamespace(call=AsyncMock(return_value={'content': [
            {'type': 'text', 'text': json.dumps({'state': 'queued'})}]}))

        async def request(rid, prompt, **kwargs):
            result = await plugin.image('generate', rid, prompt, stream_id='verify-public', **kwargs)
            assert result['success'], result
            await asyncio.gather(*plugin.tasks.values())
            return plugin.client.call.await_args.args[0]

        # 每种形态实际附图，图像摘要与登记一致，也不尝试读取任何群参考。
        for role, name in [('claude', 'Claude娘'), ('gpt', 'GPT龙娘'), ('elaina', '伊蕾娜')]:
            for form in ('standard', 'chibi'):
                submitted = await request(role + '-' + form, name + '微笑，窗边花园', form=form)
                reference = rules.AI_CARDS[role]['profiles'][form]['reference']
                assert submitted['action'] == 'edit' and submitted['references'] == [reference['file']]
                data = Path('/mnt/d/code/web/QQbot/data/image-tool/inbox', reference['file']).read_bytes()
                assert hashlib.sha256(data).hexdigest() == reference['sha256']
                assert '第1张附图' in submitted['prompt'] and '窗边花园' in submitted['prompt']
                assert '【新画背景】' in submitted['prompt']
                other_name = 'GPT娘' if role == 'claude' else 'Claude娘'
                assert other_name not in submitted['prompt']
                checked.append(role + '-' + form)
        plugin.import_reference.assert_not_awaited()

        # 回放同一任务的真实参数冲突：不允许把GPT参考送入伊蕾娜绘图请求。
        plugin.client.call.reset_mock()
        result = await plugin.image('generate', 'elaina-wrong-role', '绘制伊蕾娜穿白丝在卧室自然站立',
                                    character='gpt', stream_id='verify-public')
        assert not result['success'] and '角色名冲突' in result['error']
        plugin.client.call.assert_not_awaited()
        submitted = await request('elaina-correct-role', '伊蕾娜在卧室自然站立', character='elaina')
        assert submitted['references'] == ['elaina-standard.png']
        assert 'GPT娘（龙娘）的注册外观' not in submitted['prompt']
        checked.append('伊蕾娜参数冲突在提交前拦截')

        # Q版自动识别；同框保持名字与各自图片顺序，不因首个角色漏掉另一个。
        submitted = await request('both-chibi', 'Claude娘和GPT娘Q版一起喝茶')
        assert submitted['references'] == ['claude-chibi.webp', 'gpt-chibi.png']
        assert '第1张附图只用于确定Claude娘' in submitted['prompt']
        assert '第2张附图只用于确定GPT娘' in submitted['prompt']
        assert len(submitted['prompt']) <= 4000
        checked.append('同框Q版与附图顺序')
        submitted = await request('both-original', 'GPT娘和Claude娘普通比例替换人物', message_id='original')
        assert submitted['references'] == ['gpt-standard.jpg', 'claude-standard.jpg', 'user-original.png']
        assert '【编辑背景】' in submitted['prompt']
        checked.append('两角色加原图')
        submitted = await request('elaina-original', '将原图人物替换为伊蕾娜', character='elaina', message_id='original')
        assert submitted['references'] == ['elaina-standard.png', 'user-original.png']
        assert '第1张附图只用于确定伊蕾娜' in submitted['prompt']
        assert '【编辑背景】' in submitted['prompt']
        submitted = await request('elaina-gpt-together', '伊蕾娜和GPT娘一起喝茶', form='standard')
        assert submitted['references'] == ['elaina-standard.png', 'gpt-standard.jpg']
        assert '第2张附图只用于确定GPT娘' in submitted['prompt']
        checked.append('伊蕾娜编辑与同框参考顺序')
        submitted = await request('parent-order', 'GPT娘微笑', parent_id='old', character='gpt')
        assert '第2张附图' in submitted['prompt']
        assert submitted['parent_id'] == module.scoped_id('verify-public', 'old')
        checked.append('父图附图编号')
        plugin.client.call.reset_mock()
        result = await plugin.image('edit', 'too-many', 'Claude娘和GPT娘替换人物', message_id='original',
                                    parent_id='old', stream_id='verify-public')
        assert not result['success'] and '最多三张参考' in result['error']
        plugin.client.call.assert_not_awaited()
        checked.append('超限提交前拒绝')

        # 泛指龙和未注册的白龙形象不会套用GPT；DS娘的原有群隔离继续生效。
        submitted = await request('ordinary-dragon', '画一条龙在云中飞')
        assert submitted['action'] == 'generate' and 'references' not in submitted
        submitted = await request('ds-public-group', 'DS娘Q版微笑')
        assert submitted['action'] == 'generate' and '本群未配置固定原图' in submitted['prompt']
        checked.append('普通龙及DS跨群隔离')

        # 新角色纠正实际进入后续请求，不污染另一个角色，也不污染自己的普通版。
        plugin._ctx.message.get_by_id = AsyncMock(return_value={
            'session_id': 'verify-public', 'message_info': {'user_info': {'user_id': '123'}},
            'raw_message': [{'type': 'text', 'data': '以后Claude娘Q版头发改成粉色'}]})
        result = await plugin.image('remember', 'remember-claude', '头发改成粉色', character='claude',
                                    form='chibi', instruction_message_id='m1', stream_id='verify-public')
        assert result['success'] and result['memory']['character'] == 'claude'
        submitted = await request('learned-chibi', 'Claude娘Q版微笑')
        assert '动态修正：Claude娘' in submitted['prompt'] and '头发改成粉色' in submitted['prompt']
        submitted = await request('unaffected-gpt', 'GPT娘Q版微笑')
        assert '头发改成粉色' not in submitted['prompt']
        submitted = await request('unaffected-standard', 'Claude娘普通比例微笑')
        assert '头发改成粉色' not in submitted['prompt']
        assert plugin.memories.read('another-group', 'chibi', 'claude')['revision'] == 0
        checked.append('角色形态与群记忆隔离')
    return {'checks': checked, '真实生图': 0, 'QQ发送': 0}


if __name__ == '__main__':
    print(json.dumps(asyncio.run(verify()), ensure_ascii=False))

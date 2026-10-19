"""回放真实连续换图消息，用模拟视觉结果核对目标和字节，不调用模型或QQ。"""
import asyncio
import base64
from contextlib import redirect_stdout, redirect_stderr
import hashlib
import io
import json
import os
from pathlib import Path
import sys
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / 'MaiBot/plugins/qqbot-task-assist'))
from task_flow import PendingTask, TaskFlow, prepare_image, tool_item
from message_media import read_images


async def main():
    """数据库及图片只读，验证新请求替换旧任务并读取唯一指定的原图。"""
    os.chdir('/home/qqbot/QQbot/MaiBot')
    sys.path.insert(0, os.getcwd())
    from src.services import message_service
    from src.plugin_runtime.host.message_utils import PluginMessageUtils
    session = 'eea9ec46d0880b86983de2e4235f4bca'
    async def get(mid, chat_id='', include_binary_data=False):
        data = message_service.get_message_by_id(mid, chat_id=chat_id)
        return PluginMessageUtils._session_message_to_dict(data, include_binary_data=include_binary_data) if data else None
    ctx = SimpleNamespace(message=SimpleNamespace(get_by_id=get), logger=SimpleNamespace(warning=lambda *args:None))
    results = []
    cases = [('1791563718465.json','887744956','-1342654309','685812231'),
             ('1791563741521.json','-1342654309','423578072','-348070898')]
    for name, old_id, new_id, expected_source in cases:
        old, new = await get(old_id, chat_id=session), await get(new_id, chat_id=session)
        assert old and new
        old_stamp, new_stamp = float(old['timestamp']), float(new['timestamp'])
        assert new_stamp > old_stamp
        raw, sources = await read_images(ctx, session, new_id)
        assert sources == [expected_source], '宿主引用与预期目标不一致'
        expected_image = await asyncio.to_thread(prepare_image, raw[0])
        flow = TaskFlow()
        flow.started = old_stamp - 1
        flow.pending[session] = PendingTask(old_id, '上一张图片问题', old_stamp, True,
            {'success':True,'content':'旧图测试标记','source_request_id':old_id})
        doc = json.loads((Path('logs/maisaka_prompt/planner/qq_group_1022382380') / name).read_text())
        await flow.before(ctx, {'session_id':session,'items':doc['request_items'],
            'tool_definitions':[{'function':{'name':n}} for n in ['reply','analyze_task_image']]}, now=new_stamp+1)
        assert flow.pending[session].message_id == new_id
        assert flow.pending[session].evidence is None
        wrong = flow.after({'session_id':session,'output_items':[tool_item('analyze_task_image',{'message_id':old_id})]})
        target = wrong['output_items'][0]['tool_call']['args']['message_id']
        assert target == new_id
        visual_count = 0
        async def fake_visual(prompt, **kwargs):
            nonlocal visual_count
            visual_count += 1
            actual = [p['image_url']['url'] for p in prompt[-1]['content'] if p.get('type')=='image_url']
            assert actual == [expected_image], '视觉请求混入另一张图片'
            return {'success':True,'response':'当前图片测试标记'}
        ctx.llm = SimpleNamespace(generate=fake_visual)
        result = await flow.analyze(ctx,session,target)
        assert result['success'] and result['source_message_ids'] == [expected_source]
        reply = flow.reply_before({'session_id':session,'reply_message_id':new_id})
        assert '当前图片测试标记' in str(reply) and '旧图测试标记' not in str(reply)
        results.append({'snapshot':name,'old_task':old_id,'current_task':new_id,'source':expected_source,
            'image_sha256':hashlib.sha256(base64.b64decode(raw[0])).hexdigest(),'simulated_visual_calls':visual_count,'passed':True})
    report = {'results':results,'real_model_calls':0,'qq_messages_sent':0,'image_generations':0}
    Path('/mnt/d/code/web/QQbot/data/opencode-go/visual-target-verification.json').write_text(json.dumps(report,ensure_ascii=False,indent=2))
    return report


if __name__ == '__main__':
    with redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()):
        output = asyncio.run(main())
    print(json.dumps(output,ensure_ascii=False))

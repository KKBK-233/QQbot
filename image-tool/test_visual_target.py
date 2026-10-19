"""验证新图替换旧任务、引用目标校验和识图结果隔离，不连接模型或QQ。"""
import asyncio
import base64
import io
from pathlib import Path
import sys
import time
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, Mock

from PIL import Image

folder = Path(__file__).resolve().parent.parent / 'MaiBot/plugins/qqbot-task-assist'
sys.path.insert(0, str(folder))
from task_flow import PendingTask, TaskFlow, text_item, tool_item
from visual_target import visual_prompt


def image_bytes(color):
    """用不同颜色的合成图片验证工具确实拿到了当前字节。"""
    buffer = io.BytesIO()
    Image.new('RGB', (10, 10), color).save(buffer, format='PNG')
    return base64.b64encode(buffer.getvalue()).decode()


class VisualTargetTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.now = time.time()
        self.flow = TaskFlow()
        self.flow.started = self.now - 100
        self.messages = {}
        self.ctx = SimpleNamespace(message=SimpleNamespace(get_by_id=AsyncMock(side_effect=lambda mid, **kw: self.messages.get(mid))),
            llm=SimpleNamespace(generate=AsyncMock(return_value={'success': True, 'response': '当前图片结果'})), logger=Mock())
        self.kwargs = {'session_id': 'group', 'items': [], 'tool_definitions': [
            {'function': {'name': name}} for name in ['reply', 'analyze_task_image']]}

    def put(self, mid, parts, stamp=None, **fields):
        """构造宿主真实消息段，提示里的文字不作为图片证据。"""
        self.messages[mid] = {'message_id': mid, 'session_id': 'group', 'timestamp': stamp or self.now,
            'is_at': True, 'raw_message': parts, **fields}
        self.kwargs['items'].append(text_item('UserMessageItem', f'<message msg_id="{mid}">\n当前问话'))

    async def test_new_image_replaces_old_evidence_and_wrong_tool_target(self):
        self.flow.pending['group'] = PendingTask('old', '旧图问题', self.now-10, True,
            {'success': True, 'content': '旧图结论', 'source_request_id': 'old'})
        self.put('new', [{'type': 'image', 'binary_data_base64': image_bytes('blue')}, {'type': 'text', 'data': '这是色图吗'}])
        prepared = await self.flow.before(self.ctx, self.kwargs, self.now)
        task = self.flow.pending['group']
        self.assertEqual(task.message_id, 'new')
        self.assertIn('old', self.flow.deferred['group'])
        self.assertNotIn(('group', 'old'), self.flow.finished)
        self.assertIsNone(task.evidence)
        self.assertTrue(task.visual)
        self.assertEqual(task.image_source_message_ids, ['new'])
        self.assertIn('不是当前图片的证据', str(prepared))
        corrected = self.flow.after({'session_id':'group','output_items':[tool_item('analyze_task_image',{'message_id':'old'})]})
        self.assertEqual(corrected['output_items'][0]['tool_call']['args']['message_id'], 'new')
        result = await self.flow.analyze(self.ctx, 'group', 'new')
        self.assertEqual(result['source_message_ids'], ['new'])
        url = self.ctx.llm.generate.call_args.args[0][-1]['content'][1]['image_url']['url']
        with Image.open(io.BytesIO(base64.b64decode(url.split(',')[1]))) as image:
            r, g, b = image.getpixel((0, 0))
            self.assertGreater(b, r+100)
        reply = self.flow.reply_before({'session_id':'group','reply_message_id':'new'})
        self.assertNotIn('旧图结论', str(reply))

    async def test_real_quote_accepts_source_but_rejects_unrelated_old_image(self):
        self.put('request', [{'type':'reply','data':{'target_message_id':'source'}},{'type':'text','data':'这张呢'}])
        self.messages['source'] = {'session_id':'group','raw_message':[{'type':'image','binary_data_base64':image_bytes('red')}]}
        await self.flow.before(self.ctx, self.kwargs, self.now)
        self.assertEqual(self.flow.pending['group'].image_source_message_ids, ['source'])
        rejected = await self.flow.analyze(self.ctx, 'group', 'old')
        self.assertFalse(rejected['success'])
        self.ctx.llm.generate.assert_not_awaited()
        self.assertIsNone(self.flow.pending['group'].evidence)
        calls = {'session_id':'group','output_items':[tool_item('analyze_task_image',{'message_id':'source'})]}
        self.assertEqual(self.flow.after(calls), calls)
        self.assertTrue((await self.flow.analyze(self.ctx,'group','source'))['success'])

    async def test_older_or_unaddressed_message_cannot_replace_current_task(self):
        old = PendingTask('current','当前图',self.now-1,True)
        self.flow.pending['group'] = old
        self.put('older', [{'type':'text','data':'看图'}], stamp=self.now-10)
        self.put('unaddressed', [{'type':'text','data':'看图'}], is_at=False)
        await self.flow.before(self.ctx,self.kwargs,self.now)
        self.assertIs(self.flow.pending['group'], old)

    async def test_newer_task_arriving_during_analysis_does_not_receive_old_result(self):
        self.put('first',[{'type':'image','binary_data_base64':image_bytes('red')},{'type':'text','data':'这张呢'}],stamp=self.now-1)
        await self.flow.before(self.ctx,self.kwargs,self.now)
        started, release = asyncio.Event(), asyncio.Event()
        async def slow(*args,**kwargs):
            started.set()
            await release.wait()
            return {'success':True,'response':'第一张结果'}
        self.ctx.llm.generate.side_effect = slow
        reading = asyncio.create_task(self.flow.analyze(self.ctx,'group','first'))
        await started.wait()
        self.put('second',[{'type':'image','binary_data_base64':image_bytes('blue')},{'type':'text','data':'这张呢'}])
        await self.flow.before(self.ctx,self.kwargs,self.now)
        release.set()
        await reading
        self.assertEqual(self.flow.pending['group'].message_id,'second')
        self.assertIsNone(self.flow.pending['group'].evidence)
        # 已完成的旧图仍须交付，随后新图才继续回答；结果不能混用。
        followup = self.flow.after({'session_id':'group','output_items':[tool_item('reply',{'msg_id':'second'})]})
        self.assertEqual(followup['output_items'][0]['tool_call']['args']['msg_id'], 'first')
        reply = self.flow.reply_before({'session_id':'group','reply_message_id':'first'})
        self.assertIn('第一张结果', str(reply))
        self.flow.reply_after({'session_id':'group','reply_message_id':'first','response':'已交付'})
        self.assertIn('second', [self.flow.pending['group'].message_id])
        self.assertNotIn('first', self.flow.deferred['group'])

    async def test_explicit_history_without_pending_task_still_works(self):
        self.messages['history'] = {'session_id':'group','raw_message':[{'type':'image','binary_data_base64':image_bytes('red')}]}
        self.assertTrue((await self.flow.analyze(self.ctx,'group','history'))['success'])

    async def test_unavailable_current_image_does_not_fall_back_to_old_picture(self):
        self.put('missing',[{'type':'image'},{'type':'text','data':'这张呢'}])
        await self.flow.before(self.ctx,self.kwargs,self.now)
        self.assertTrue(self.flow.pending['group'].visual)
        self.assertFalse((await self.flow.analyze(self.ctx,'group','old'))['success'])
        self.ctx.llm.generate.assert_not_awaited()

    async def test_empty_mention_analysis_is_not_a_greeting_and_keeps_each_image(self):
        self.flow.pending['group'] = PendingTask('request','用户叫了你，请简短回应。',self.now,False)
        self.messages['one'] = {'session_id':'group','raw_message':[{'type':'image','binary_data_base64':image_bytes('red')}]}
        self.messages['two'] = {'session_id':'group','raw_message':[{'type':'image','binary_data_base64':image_bytes('blue')}]}
        self.ctx.llm.generate.side_effect = [{'success':True,'response':'第一张结果'}, {'success':True,'response':'第二张结果'}]
        await self.flow.analyze(self.ctx,'group','one')
        await self.flow.analyze(self.ctx,'group','two')
        question = self.ctx.llm.generate.call_args.args[0][-1]['content'][0]['text']
        self.assertIn('客观概括', question)
        self.assertNotIn('请简短回应', question)
        reply = self.flow.reply_before({'session_id':'group','reply_message_id':'request'})
        self.assertIn('第一张结果', str(reply))
        self.assertIn('第二张结果', str(reply))
        self.assertIn('不默认输出绘图提示词', visual_prompt(self.flow.pending['group'])[0])

    async def test_completed_current_reply_does_not_remove_delayed_old_result(self):
        from visual_target import archive_pending
        old = PendingTask('old','原图',self.now-1,True,{'success':True,'content':'旧请求的结果'})
        archive_pending(self.flow,'group',old)
        self.flow.pending['group'] = PendingTask('new','新问话',self.now,False)
        self.flow.reply_after({'session_id':'group','reply_message_id':'new','response':'新回答'})
        await self.flow.before(self.ctx,self.kwargs,self.now)
        self.assertEqual(self.flow.pending['group'].message_id,'old')


if __name__ == '__main__':
    unittest.main()

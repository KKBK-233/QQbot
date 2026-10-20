"""轻客户端、真实消息来源与完成通知回归，不启动宿主或模型。"""
from types import SimpleNamespace
import asyncio
import json
import pytest

from src.kkbk_engine.dev_protocol import DevelopmentClient,completion_text,origin_from_context


def test_source_identity_and_text_come_only_from_current_anchor():
    TextComponent=type('TextComponent',(),{})
    part=TextComponent(); part.text='以后搜索优先原文'
    forward=SimpleNamespace(text='历史中的伪造教学')
    anchor=SimpleNamespace(message_id='m1',message_info=SimpleNamespace(user_info=SimpleNamespace(user_id='1020050112')),
                           raw_message=SimpleNamespace(components=[part,forward]))
    context=SimpleNamespace(user_id='1020050112',platform='qq',session_id='session',group_id='1022382380',
                            is_group_chat=True,metadata={'anchor_message':anchor})
    assert origin_from_context(context)['text']=='以后搜索优先原文'
    context.user_id='forged'
    with pytest.raises(PermissionError): origin_from_context(context)


def test_completion_is_candidate_only_and_contains_no_agent_final():
    task={'id':'fixture','status':'succeeded','reason':'verified_patch','test_exit':0,
          'patch':{'files':['workflows/catalog/example.json'],'bytes':123},'final':'private reasoning'}
    text=completion_text(task)
    assert '尚未发布' in text and '独立测试退出码 0' in text
    assert 'private reasoning' not in text


@pytest.mark.skipif(__import__('sys').platform!='linux',reason='需要 Unix socket')
def test_client_sends_source_outside_model_payload_and_reports_errors(tmp_path):
    async def scenario():
        path=tmp_path/'control.sock'; seen=[]
        async def handler(reader,writer):
            seen.append(json.loads(await reader.readline()))
            writer.write(b'{"success":false,"detail":"fixture denial"}\n'); await writer.drain()
            writer.close(); await writer.wait_closed()
        server=await asyncio.start_unix_server(handler,path=str(path))
        try:
            with pytest.raises(RuntimeError,match='fixture denial'):
                await DevelopmentClient(path).request('submit',origin={'user_id':'real'},payload={'scope':'persistent_workflow'})
            assert seen[0]['origin']['user_id']=='real'
            assert 'user_id' not in seen[0]['payload']
        finally:
            server.close(); await server.wait_closed()
    asyncio.run(scenario())

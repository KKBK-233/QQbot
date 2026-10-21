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


def test_completion_reports_release_outcome():
    task={'id':'fixture','status':'succeeded','reason':'verified_patch','test_exit':0,
          'patch':{'files':['workflows/enabled.json'],'bytes':10}}
    installed=completion_text({**task,'release':{'state':'installed','release_id':'r1',
        'workflows':['conservative-web-research@1.1.0'],'error':'','restore_error':''}})
    assert '已自动发布：conservative-web-research@1.1.0' in installed and 'r1' in installed
    assert '尚未发布' not in installed
    rolled=completion_text({**task,'release':{'state':'rolled_back','release_id':'r2','workflows':[],
        'error':'ReleaseError: 线上加载结果与候选不一致','restore_error':''}})
    assert '自动恢复原版本' in rolled and '不一致' in rolled
    failed=completion_text({**task,'release':{'state':'failed','release_id':'r3','workflows':[],
        'error':'ReleaseError: 线上工作流在候选生成后已变化','restore_error':''}})
    assert '未发布：' in failed


def test_continuation_receipt_distinguishes_round_and_queued_messages():
    task={'id':'round-two','status':'succeeded','reason':'verified_patch','test_exit':0,
          'conversation':{'root_task_id':'original-task','queued_messages':1}}
    text=completion_text(task)
    assert '所属开发会话：original-task' in text
    assert '不代表后续轮次已执行' in text
    assert '尚未发布' in text


def test_development_message_tool_preserves_host_identity_and_has_no_session_override():
    """执行真实提供器方法，验证续聊仍经过当前宿主来源而非模型伪造会话。"""
    from pathlib import Path
    from unittest.mock import AsyncMock
    import ast
    from src.kkbk_engine.config import EngineConfig
    path=Path(__file__).resolve().parents[2]/'src/kkbk_engine/development.py'
    klass=next(node for node in ast.parse(path.read_text(encoding='utf-8')).body if isinstance(node,ast.ClassDef))
    client=SimpleNamespace(request=AsyncMock(return_value={'message_queued':True}))
    ns={'json':json,'DevelopmentClient':lambda:client,'origin_from_context':origin_from_context,
        'ToolSpec':lambda **kwargs:SimpleNamespace(**kwargs),
        'ToolExecutionResult':lambda **kwargs:SimpleNamespace(**kwargs)}
    exec(compile(ast.Module(body=[klass],type_ignores=[]),str(path),'exec'),ns)
    provider=ns['DevelopmentToolProvider'](SimpleNamespace(config=EngineConfig()))
    component=type('TextComponent',(),{})(); component.text='继续完善原任务'
    context=SimpleNamespace(platform='qq',is_group_chat=True,group_id='427204073',session_id='other-group-session',
        user_id='1020050112',metadata={'anchor_message':SimpleNamespace(message_id='actual-message',
            message_info=SimpleNamespace(user_info=SimpleNamespace(user_id='1020050112')),
            raw_message=SimpleNamespace(components=[component]))})
    specs=asyncio.run(provider.list_tools(context))
    spec=next(spec for spec in specs if spec.name=='development_message')
    assert set(spec.parameters_schema['properties'])=={'task_id','message'}
    invocation=SimpleNamespace(tool_name='development_message',arguments={'task_id':'old-task','message':'补齐验收'})
    result=asyncio.run(provider.invoke(invocation,context))
    assert result.success
    assert client.request.await_args.args==('message',)
    assert client.request.await_args.kwargs['origin']['message_id']=='actual-message'
    assert client.request.await_args.kwargs['origin']['group_id']=='427204073'
    assert client.request.await_args.kwargs['origin']['session_id']=='other-group-session'
    invocation.arguments['session_id']='forged'
    assert not asyncio.run(provider.invoke(invocation,context)).success
    assert client.request.await_count==1
    context.user_id='other-user'
    assert asyncio.run(provider.list_tools(context))==[]


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

"""在宿主环境验证开发工具接线与真实 socket 查询，不提交任务或发送群消息。"""
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock
import argparse
import asyncio
import json
import os
import sys

ROOT=Path('/home/qqbot/QQbot/MaiBot')


async def main():
    parser=argparse.ArgumentParser(); parser.add_argument('--stage',action='store_true')
    args=parser.parse_args()
    os.chdir(ROOT); sys.path.insert(0,str(ROOT))
    if args.stage:
        import src
        src.__path__.insert(0,'/home/qqbot/QQbot/kkbk-engine-stage/src')
    from src.common.data_models.message_component_data_model import TextComponent
    from src.core.tooling import ToolAvailabilityContext,ToolExecutionContext,ToolInvocation
    from src.kkbk_engine.config import EngineConfig
    from src.kkbk_engine.development import DevelopmentToolProvider
    from src.kkbk_engine.dev_protocol import DevelopmentClient
    provider=DevelopmentToolProvider(SimpleNamespace(config=EngineConfig()))
    context=ToolAvailabilityContext(platform='qq',is_group_chat=True,group_id='1022382380',
                                    session_id='offline-development',user_id='1020050112')
    specs=await provider.list_tools(context)
    assert {spec.name for spec in specs}=={'learn_teaching','development_submit','development_status',
                                           'development_cancel','development_rollback','development_message'}
    for spec in specs: spec.to_llm_definition()
    # 每个授权群都必须保留完整入口；私聊和未知群仍不获得管理工具。
    for group_id in EngineConfig().identity.group_whitelist:
        context.group_id=group_id
        assert {spec.name for spec in await provider.list_tools(context)}=={spec.name for spec in specs}
    context.group_id='not-allowed'; assert await provider.list_tools(context)==[]
    context.group_id='427204073'; context.is_group_chat=False
    assert await provider.list_tools(context)==[]
    context.is_group_chat=True
    context.user_id='not-admin'; assert await provider.list_tools(context)==[]
    anchor=SimpleNamespace(message_id='fixture',message_info=SimpleNamespace(user_info=SimpleNamespace(user_id='1020050112')),
                           raw_message=SimpleNamespace(components=[TextComponent(text='以后搜索优先官方原文')]))
    execution=ToolExecutionContext(platform='qq',is_group_chat=True,group_id='1022382380',
                                  session_id='offline-development',user_id='1020050112',metadata={'anchor_message':anchor})
    provider.client.request=AsyncMock(return_value={'id':'fixture','status':'queued'})
    result=await provider.invoke(ToolInvocation(tool_name='development_submit',arguments={
        'scope':'persistent_workflow','instructions':'fixture','acceptance':'fixture'}),execution)
    assert result.success
    assert provider.client.request.call_args.kwargs['origin']['message_id']=='fixture'
    assert provider.client.request.call_args.kwargs['origin']['text']=='以后搜索优先官方原文'
    result=await provider.invoke(ToolInvocation(tool_name='development_message',arguments={
        'task_id':'fixture','message':'继续同一会话'}),execution)
    assert result.success
    assert provider.client.request.call_args.args[0]=='message'
    assert provider.client.request.call_args.kwargs['origin']['message_id']=='fixture'
    result=await provider.invoke(ToolInvocation(tool_name='development_submit',arguments={
        'scope':'persistent_workflow','instructions':'fixture','acceptance':'fixture','origin':{'user_id':'forged'}}),execution)
    assert not result.success
    if not args.stage:
        response=await DevelopmentClient().request('notifications',payload={'session_id':'offline-development-no-delivery'})
        assert response is None
    print(json.dumps({'admin_tools':6,'non_admin_tools':0,
                      'authorized_groups_checked':len(EngineConfig().identity.group_whitelist),'private_tools':0,
                      'source_bound':True,'conversation_message_bound':True,
                      'model_origin_rejected':True,'real_socket_checked':not args.stage}))


if __name__=='__main__': asyncio.run(main())

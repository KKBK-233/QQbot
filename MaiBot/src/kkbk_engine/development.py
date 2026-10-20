"""管理员专用的开发协调工具，调用独立服务而非在聊天进程执行代码。"""
import json

from src.core.tooling import ToolExecutionResult,ToolSpec
from .dev_protocol import DevelopmentClient,origin_from_context


class DevelopmentToolProvider:
    """身份和会话来自宿主上下文；任何群主身份或聊天自称都不能授权。"""
    provider_name='kkbk_development'
    provider_type='builtin'

    def __init__(self, runtime):
        self.runtime=runtime
        self.client=DevelopmentClient()

    def allowed(self, context):
        identity=self.runtime.config.identity
        return (context is not None and context.platform=='qq' and context.is_group_chat
                and context.user_id in identity.operator_user_ids and context.group_id in identity.group_whitelist)

    async def list_tools(self, context=None):
        if not self.allowed(context): return []
        schemas={
            'development_submit':('管理员明确要求以后长期这样做，或要求修改代码/插件时，提交异步开发候选。'
                '仅本次的指导使用普通任务工具，不提交开发。scope=持久工作流或代码修改；'
                '只排队，不发布；同一原始消息重复调用返回同一任务。',{
                'scope':{'type':'string','enum':['persistent_workflow','code_change']},
                'instructions':{'type':'string','maxLength':8192},
                'acceptance':{'type':'string','maxLength':4096}},['scope','instructions','acceptance']),
            'development_status':('查询当前管理员在当前群的开发任务；省略 task_id 列出最近任务。',{
                'task_id':{'type':'string'}},[]),
            'development_cancel':('取消当前管理员在当前群的指定开发任务，已完成的任务不回滚。',{
                'task_id':{'type':'string'}},['task_id']),
        }
        return [ToolSpec(name=name,description=description,provider_name=self.provider_name,
                    provider_type=self.provider_type,parameters_schema={'type':'object','properties':properties,
                    'required':required,'additionalProperties':False})
                for name,(description,properties,required) in schemas.items()]

    async def invoke(self, invocation, context=None):
        try:
            if not self.allowed(context): raise PermissionError('仅当前开发管理员可操作')
            origin=origin_from_context(context)
            args=invocation.arguments
            allowed={'development_submit':{'scope','instructions','acceptance'},
                     'development_status':{'task_id'},'development_cancel':{'task_id'}}
            if invocation.tool_name not in allowed or set(args)-allowed[invocation.tool_name]:
                raise ValueError('开发工具参数包含未开放字段')
            operation={'development_submit':'submit','development_status':'status',
                       'development_cancel':'cancel'}[invocation.tool_name]
            if operation=='status' and not args.get('task_id'): operation='list'
            result=await self.client.request(operation,origin=origin,payload=args)
            return ToolExecutionResult(tool_name=invocation.tool_name,success=True,
                    content=json.dumps(result,ensure_ascii=False),structured_content=result)
        except (OSError,ValueError,PermissionError,RuntimeError,TimeoutError) as exc:
            return ToolExecutionResult(tool_name=invocation.tool_name,success=False,
                    error_message='开发请求未完成：'+str(exc)[:256])

    async def close(self):
        """客户端无常驻连接，服务生命周期由独立进程管理。"""

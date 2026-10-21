"""管理员专用的开发协调工具，调用独立服务而非在聊天进程执行代码。"""
import json

from src.core.tooling import ToolExecutionResult,ToolSpec
from .dev_protocol import DevelopmentClient,origin_from_context
from .teaching import TeachingMemory


class DevelopmentToolProvider:
    """身份和会话来自宿主上下文；任何群主身份或聊天自称都不能授权。"""
    provider_name='kkbk_development'
    provider_type='builtin'

    def __init__(self, runtime):
        self.runtime=runtime
        self.client=DevelopmentClient()
        self._teaching=None

    @property
    def teaching(self):
        """沿用现有原生记忆后端，不修改个人事实与普通记忆处理器。"""
        if self._teaching is None:
            from src.services.memory_service import memory_service
            self._teaching=TeachingMemory(memory_service,identity=self.runtime.config.identity)
        return self._teaching

    def allowed(self, context):
        identity=self.runtime.config.identity
        return (context is not None and context.platform=='qq' and context.is_group_chat
                and context.user_id in identity.operator_user_ids and context.group_id in identity.group_whitelist)

    async def list_tools(self, context=None):
        if not self.allowed(context): return []
        schemas={
            'learn_teaching':('识别管理员在教你方法时主动学习，不要求他说“记住”。先整理可复用规则和关键词；'
                '准备回复“我记住了”时调用此工具，落库后宿主会发送你的复述和关键词。'
                '无需逐字摘录，不把教学方法当个人事实；支持多条管理员消息。',{
                'summary':{'type':'string','maxLength':2000},
                'keywords':{'type':'array','items':{'type':'string','maxLength':48},'minItems':1,'maxItems':12},
                'source_message_ids':{'type':'array','items':{'type':'string'},'maxItems':20}},['summary','keywords']),
            'development_submit':('管理员的教学需要改工作流执行或源码/插件时，提交异步开发候选。'
                '由你判断需要的作用范围，不要求原话出现“以后”或“开发”等口令。'
                '仅本次的指导使用普通任务工具，不提交开发。scope=持久工作流或代码修改；'
                '持久工作流候选通过测试后由宿主自动发布，代码修改只保留候选、由维护端部署；'
                '管理员要求发布代码候选时直接说明需维护端部署，不要用 development_message 让开发代理去发布；'
                '同一原始消息重复调用返回同一任务。',{
                'scope':{'type':'string','enum':['persistent_workflow','code_change']},
                'instructions':{'type':'string','maxLength':8192},
                'acceptance':{'type':'string','maxLength':4096}},['scope','instructions','acceptance']),
            'development_status':('查询当前管理员在任一已授权群提交的开发任务及 conversation 实际会话进度、代理总结、错误和工具调用计数；'
                '允许跨群查询，查询不会迁移完成通知。source_group_id 为原群，notification_group_id 为当前轮次通知群；'
                'summary_task_id 表示总结所属轮次。省略 task_id 列出最近任务。',{
                'task_id':{'type':'string'}},[]),
            'development_message':('同一管理员可从其他已授权群向已有开发会话补充需求、回答问题或要求继续。沿用原目标、DSH 会话与工作副本，'
                '续聊轮次完成通知发到本次发起续聊的群；已经运行的旧轮次通知仍留在原群。'
                '当前轮结束后执行，不即时打断工具。先查 development_status 获取真实阶段和代理总结；'
                '用同一目标的任务编号，不重新 submit。只有接口确认受理后才能说已补充。',{
                'task_id':{'type':'string'},'message':{'type':'string','maxLength':4096}},['task_id','message']),
            'development_cancel':('取消当前管理员在当前群的指定开发任务，已完成的任务不回滚。',{
                'task_id':{'type':'string'}},['task_id']),
            'development_rollback':('管理员明确要求撤销最近一次自动发布的工作流时调用，恢复发布前的线上版本；'
                '发布之后线上又有改动时宿主会拒绝。',{},[]),
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
            allowed={'learn_teaching':{'summary','keywords','source_message_ids'},
                     'development_submit':{'scope','instructions','acceptance'},
                     'development_status':{'task_id'},'development_message':{'task_id','message'},'development_cancel':{'task_id'},
                     'development_rollback':set()}
            if invocation.tool_name not in allowed or set(args)-allowed[invocation.tool_name]:
                raise ValueError('开发工具参数包含未开放字段')
            if invocation.tool_name=='learn_teaching':
                result=await self.teaching.learn(origin,self.runtime.messages,args)
                return ToolExecutionResult(tool_name=invocation.tool_name,success=True,
                    content=json.dumps(result,ensure_ascii=False),structured_content=result,
                    metadata={'teaching_receipt':result['reply']})
            operation={'development_submit':'submit','development_status':'status','development_message':'message',
                       'development_cancel':'cancel','development_rollback':'rollback'}[invocation.tool_name]
            if operation=='status' and not args.get('task_id'): operation='list'
            result=await self.client.request(operation,origin=origin,payload=args)
            return ToolExecutionResult(tool_name=invocation.tool_name,success=True,
                    content=json.dumps(result,ensure_ascii=False),structured_content=result)
        except (OSError,ValueError,PermissionError,RuntimeError,TimeoutError) as exc:
            return ToolExecutionResult(tool_name=invocation.tool_name,success=False,
                    error_message='开发请求未完成：'+str(exc)[:256])

    async def close(self):
        """客户端无常驻连接，服务生命周期由独立进程管理。"""

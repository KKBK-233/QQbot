"""本机宿主开发协调协议：实际来源、教学作用范围、去重与完成回执。"""
import hashlib
import json

ALLOWED_GROUPS=frozenset({'1022382380','894179464','427204073'})


def text(value, label, maximum=8192):
    if not isinstance(value,str) or not value.strip() or len(value.encode('utf-8'))>maximum:
        raise ValueError(label+' 必须为有界非空文本')
    return value.strip()


class Coordinator:
    """Unix socket 的同用户宿主是信任边界；模型参数不能提供来源身份。"""
    def __init__(self, worker):
        self.worker=worker
        self.store=worker.store

    def authorize(self, origin):
        if not isinstance(origin,dict): raise PermissionError('缺少宿主来源')
        if origin.get('platform')!='qq' or not self.worker.config.is_allowed(origin.get('user_id')):
            raise PermissionError('仅限真实 QQ 开发管理员')
        if origin.get('group_id') not in ALLOWED_GROUPS: raise PermissionError('群不在已配置范围')
        for key in ('session_id','message_id','user_id','group_id'): text(origin.get(key),key,256)
        text(origin.get('text'),'当前消息',65536)
        return origin

    def project(self, task):
        """群内只返回有界结果；不返回任务正文、思考、原始日志或凭据文件。"""
        result=task['result']; raw_patch=result.get('patch') or {}
        names=raw_patch.get('files',[])
        patch=({'files':[name[:256] for name in names[:20]],'file_count':len(names),
                'sha256':raw_patch.get('sha256'),'bytes':raw_patch.get('bytes')}
               if raw_patch else {})
        return self.worker.redactor.value({
            'id':task['id'],'status':task['status'],'phase':task['phase'],'reason':task['reason'],
            'cancel_requested':task['cancel_requested'],'scope':task['spec'].get('scope','code_change'),
            'base_commit':task['spec']['base_commit'],'candidate_only':True,
            'candidate_discarded':bool(result.get('candidate_discarded')),
            'test_exit':result.get('test_exit'),'patch':patch,
            'error':result.get('error',''),'delivery':self.store.notification_state(task['id'])})

    def owned(self, task_id, origin):
        task=self.store.get(task_id)
        source=task['spec'].get('origin',{})
        if source.get('session_id')!=origin['session_id'] or source.get('user_id')!=origin['user_id']:
            raise PermissionError('只能管理当前会话中自己提交的开发任务')
        return task

    def submit(self, origin, payload):
        scope=payload.get('scope')
        raw=origin['text']
        # 教师账号已由宿主核验，作用范围由模型理解，不再要求固定教学口令。
        if scope not in ('persistent_workflow','code_change'): raise ValueError('本次操作不生成持久开发任务')
        instructions=text(payload.get('instructions'),'需求')
        acceptance=text(payload.get('acceptance'),'验收条件',4096)
        request_key=hashlib.sha256(json.dumps([origin[key] for key in ('platform','session_id','message_id')]).encode()).hexdigest()
        prompt=('这是宿主验证过的管理员开发需求。只生成候选补丁与测试证据，不发布。\n'
                +'作用范围：'+scope+'\n原始教学：'+raw+'\n协调需求：'+instructions+'\n验收条件：'+acceptance)
        if scope=='persistent_workflow':
            prompt+='\n只允许修改 workflows/ 与 pytests/kkbk_engine/。生成新版本并更新启用清单，验证加载及快照行为。'
        task=self.worker.submit(origin['user_id'],prompt,request_key=request_key,origin=origin,scope=scope)
        return self.project(task)

    def dispatch(self, request):
        if not isinstance(request,dict) or request.get('version')!=1: raise ValueError('控制协议版本错误')
        operation=request.get('operation'); payload=request.get('payload',{})
        if not isinstance(payload,dict): raise ValueError('payload 必须为对象')
        if operation in ('notifications','acknowledge'):
            session=text(payload.get('session_id'),'session_id',256)
            if operation=='acknowledge':
                return {'acknowledged':self.store.acknowledge_notification(session,payload.get('id'),
                         payload.get('delivery_nonce'),payload.get('receipt'))}
            self.store.queue_notifications()
            item=self.store.take_notification(session)
            if item is None: return None
            # 协议确认随机值需要传给本机宿主，不使用会被通用凭据脱敏的 token 字段名。
            return {'id':item['id'],'task_id':item['task_id'],'session_id':item['session_id'],
                    'delivery_nonce':item['claim_token'],'task':self.project(self.store.get(item['task_id']))}
        origin=self.authorize(request.get('origin'))
        if operation=='submit': return self.submit(origin,payload)
        if operation=='list':
            tasks=[]
            for entry in self.store.list():
                try: tasks.append(self.project(self.owned(entry['id'],origin)))
                except PermissionError: pass
            return tasks[:10]
        if operation in ('status','cancel'):
            task=self.owned(payload.get('task_id'),origin)
            if operation=='cancel': task=self.store.cancel(task['id'])
            return self.project(task)
        raise ValueError('不支持的控制操作')

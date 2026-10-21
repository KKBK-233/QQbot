"""开发服务的轻量本机客户端，不导入代理、凭据或运行源码。"""
from pathlib import Path
import asyncio
import json
import os


class DevelopmentClient:
    """单次有界请求；不对可能已提交成功的请求自动重试。"""
    def __init__(self, path=None):
        self.path=Path(path or os.environ.get('KKBK_DEV_WORKER_SOCKET',
                                             '/home/qqbot/dev-harness/run/control.sock'))

    async def request(self, operation, *, origin=None, payload=None):
        async def exchange():
            reader,writer=await asyncio.open_unix_connection(str(self.path),limit=131072)
            try:
                message={'version':1,'operation':operation,'payload':payload or {}}
                if origin is not None: message['origin']=origin
                raw=(json.dumps(message,ensure_ascii=False)+'\n').encode()
                if len(raw)>131072: raise ValueError('开发请求过大')
                writer.write(raw); await writer.drain()
                line=await reader.readline()
                if not line.endswith(b'\n'): raise RuntimeError('开发服务响应不完整')
                response=json.loads(line)
                if response.get('success') is not True:
                    raise RuntimeError(response.get('detail','开发服务请求失败'))
                return response['result']
            finally:
                writer.close(); await writer.wait_closed()
        return await asyncio.wait_for(exchange(),timeout=8)


def origin_from_context(context):
    """仅取宿主 anchor 的顶层文本和结构化账号，忽略模型传入的身份字段。"""
    anchor=context.metadata.get('anchor_message')
    info=anchor.message_info.user_info if anchor else None
    if info is None or str(info.user_id)!=context.user_id or not context.is_group_chat:
        raise PermissionError('缺少可验证的当前消息')
    raw=''.join(part.text for part in anchor.raw_message.components
                if type(part).__name__=='TextComponent')
    return {'platform':context.platform,'session_id':context.session_id,'group_id':context.group_id,
            'message_id':str(anchor.message_id),'user_id':str(info.user_id),'text':raw}


def completion_text(task):
    """完成通知只展示候选状态、补丁范围与独立测试，不输出模型自述。"""
    labels={'succeeded':'候选补丁已验证','failed':'开发未完成','timeout':'开发已超时','cancelled':'开发已取消'}
    lines=[f"开发任务 {task['id']}：{labels.get(task['status'],task['status'])}。"]
    conversation=task.get('conversation') or {}
    if conversation.get('root_task_id') and conversation['root_task_id']!=task['id']:
        lines.append('所属开发会话：'+conversation['root_task_id'])
    if conversation.get('queued_messages'):
        lines.append('已有补充消息排队；本轮结果不代表后续轮次已执行。')
    patch=task.get('patch') or {}
    if patch.get('files'):
        lines.append('改动：'+'、'.join(patch['files'][:6]))
        lines.append(f"补丁 {patch.get('bytes',0)} 字节；独立测试退出码 {task.get('test_exit')}。")
    if task.get('reason') not in (None,'verified_patch'): lines.append('原因：'+str(task['reason']))
    # 工作流候选会自动发布；其余候选只留在开发工作副本。
    published=task.get('release')
    if not published:
        lines.append('结果保留在开发工作副本，尚未发布。')
    elif published['state']=='installed':
        lines.append('已自动发布：'+'、'.join(published['workflows'][:6])+f"（发布记录 {published['release_id']}）。")
        lines.append('新任务使用新版本；如需撤销可让我回退最近一次发布。')
    elif published['state']=='rolled_back':
        lines.append('发布自检失败，线上已自动恢复原版本：'+published['error'][:300])
    elif published['state']=='restore_failed':
        lines.append('发布自检失败且自动恢复失败，需要人工检查：'+published['restore_error'][:300])
    else:
        lines.append('未发布：'+published['error'][:300])
    return '\n'.join(lines)[:1400]

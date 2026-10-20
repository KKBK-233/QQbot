"""阶段二本地开发执行器：固定提交、独立验证、产物，不自动发布。"""
from collections import Counter
from dataclasses import replace
from pathlib import Path
import json
import re
import time

from .config import WorkerConfig
from .process import Stopped, run_process
from .store import Store, atomic_json, exclusive_lock
from .workspace import (agent_command,agent_env,collect_patch,prepare_clone,prepare_environment,
                        resolve_commit,test_command)

def event_summary(path: Path):
    """读取 DSH 事件，不把 final 自述或退出码单独视为测试成功。"""
    tools=Counter()
    final=''
    session=None
    usage=None
    errors=[]
    if path.exists():
        for line in path.read_text(encoding='utf-8',errors='replace').splitlines():
            try: event=json.loads(line)
            except ValueError: continue
            if not isinstance(event,dict): continue
            kind=event.get('type')
            if kind=='session': session=event.get('sessionId')
            if kind=='tool_call': tools[str(event.get('tool','unknown'))]+=1
            if kind=='final': final=str(event.get('text',''))[-8192:]
            if kind=='error':
                message=str(event.get('message',event.get('error','')))[:512]
                errors.append(re.sub(r'\bsk-[A-Za-z0-9_-]+|gh[pousr]_[A-Za-z0-9]+','[credential]',message))
            if isinstance(event.get('usage'),dict): usage=event['usage']
    final=re.sub(r'\bsk-[A-Za-z0-9_-]+|gh[pousr]_[A-Za-z0-9]+','[credential]',final)
    return {'session_id':session,'final':final,'tool_calls':dict(tools),
            'last_reported_usage':usage,'event_errors':errors[-5:]}

def outcome(agent_exit,patch_bytes,test_exit,events):
    if agent_exit != 0: return 'failed','agent_failed'
    if events.get('event_errors'): return 'failed','agent_event_error'
    if not patch_bytes: return 'failed','no_changes'
    if test_exit != 0: return 'failed','tests_failed'
    return 'succeeded','verified_patch'

class Worker:
    def __init__(self,config: WorkerConfig):
        config.validate()
        self.config=config
        self.store=Store(config.root)

    def submit(self,requester,task,base_ref=None):
        # 这里只是本地操作员 CLI 的白名单；远程身份认证在阶段三接入。
        if not self.config.is_allowed(requester): raise PermissionError('请求者不在开发管理员名单')
        if not isinstance(task,str) or not task.strip() or len(task.encode('utf-8'))>65536:
            raise ValueError('任务必须为非空文本且不超过 64 KiB')
        ref=base_ref or self.config.base_ref
        commit=resolve_commit(self.config,ref)
        return self.store.submit({'requester':requester,'task':task,'base_ref':ref,
                                  'base_commit':commit,'test_command':self.config.test_command,
                                  'source_repo':str(self.config.source_repo.resolve())})

    def run_once(self):
        """持锁恢复中断状态，再原子领取一项；没有自动重试或发布。"""
        with exclusive_lock(self.config.lock_path):
            self.store.recover()
            task=self.store.claim()
            if task is None: return None
            return self.execute(task)

    def execute(self,task):
        cfg=self.config
        task_id=task['id']
        folder=self.store.directory(task_id)
        started=time.monotonic()
        result={'base_commit':task['spec']['base_commit'],'agent_exit':None,'test_exit':None,
                'patch':None,'events':{},'durations':{},'artifacts':{}}
        status,reason='failed','worker_error'
        cancelled=lambda:self.store.get(task_id)['cancel_requested']
        interrupted=False
        try:
            if str(cfg.source_repo.resolve())!=task['spec']['source_repo']:
                raise ValueError('任务提交后源码仓库路径发生变化')
            for phase,operation in [
                ('clone',lambda:prepare_clone(cfg,folder,task['spec']['base_commit'],cancelled)),
                ('environment',lambda:prepare_environment(cfg,folder,cancelled))]:
                self.store.phase(task_id,phase)
                before=time.monotonic(); operation()
                result['durations'][phase]=round(time.monotonic()-before,3)
            self.store.phase(task_id,'agent')
            response=run_process(agent_command(cfg,folder,task['spec']['task']),cwd=folder/'ws',
                env=agent_env(cfg,folder),stdout=folder/'events.jsonl',stderr=folder/'stderr.log',
                timeout=cfg.agent_timeout_sec,cancel=cancelled,max_bytes=cfg.max_log_bytes,
                idle_timeout=cfg.agent_idle_timeout_sec)
            result['agent_exit']=response.exit_code
            result['durations']['agent']=round(response.seconds,3)
            result['events']=event_summary(folder/'events.jsonl')
            self.store.phase(task_id,'collect')
            patch=collect_patch(cfg,folder,task['spec']['base_commit'],cancelled)
            result['patch']=patch
            if patch['bytes']:
                self.store.phase(task_id,'test')
                test_cfg=replace(cfg,test_command=task['spec']['test_command'])
                response=run_process(test_command(test_cfg,folder),cwd=folder/'ws',env=agent_env(cfg,folder),
                    stdout=folder/'test.log',stderr=folder/'test.err',timeout=cfg.test_timeout_sec,
                    cancel=cancelled,max_bytes=cfg.max_log_bytes)
                result['test_exit']=response.exit_code
                result['durations']['test']=round(response.seconds,3)
                # 测试产生的实际源码变化也必须披露，不能把测试前补丁当作最终版本。
                after=collect_patch(cfg,folder,task['spec']['base_commit'],cancelled)
                result['patch']=after
                if after['sha256']!=patch['sha256']:
                    raise RuntimeError('workspace_changed_during_tests')
            status,reason=outcome(result['agent_exit'],patch['bytes'],result['test_exit'],result['events'])
        except Stopped as exc:
            reason=exc.reason
            status='cancelled' if reason=='cancelled' else 'timeout' if reason=='timeout' else 'failed'
        except KeyboardInterrupt:
            status,reason,interrupted='cancelled','worker_stopped',True
        except Exception as exc:
            # 仅记录异常类型与受控消息；原始工具日志留在私有开发目录。
            result['error']=type(exc).__name__+': '+str(exc)[:512]
            status,reason='failed','worker_error'
        finally:
            result['durations']['total']=round(time.monotonic()-started,3)
            for name in ('events.jsonl','stderr.log','patch.diff','test.log','test.err'):
                if (folder/name).exists(): result['artifacts'][name]=name
            self.store.finish(task_id,status,reason,result)
            completed=self.store.get(task_id)
            atomic_json(folder/'summary.json',completed)
        if interrupted: raise KeyboardInterrupt
        return completed

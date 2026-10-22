"""本地开发执行器：固定提交、独立验证、产物；工作流候选通过测试后自动发布。"""
from collections import Counter
from dataclasses import replace
from pathlib import Path
import json
import time

from . import project, release, source_release
from .config import WorkerConfig
from .process import Stopped, run_process
from .redaction import Redactor
from .store import Store, atomic_json, exclusive_lock
from .workspace import (agent_command,agent_env,collect_patch,prepare_clone,prepare_environment,
                        resolve_commit,test_command)

def event_summary(path: Path, redactor=None):
    """读取 DSH 事件，不把 final 自述或退出码单独视为测试成功。"""
    redactor=redactor or Redactor()
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
            if kind=='final': final=redactor.text(str(event.get('text','')))[-8192:]
            if kind=='error':
                message=redactor.text(str(event.get('message',event.get('error',''))))[:512]
                errors.append(message)
            if isinstance(event.get('usage'),dict): usage=event['usage']
    return redactor.value({'session_id':session,'final':final,'tool_calls':dict(tools),
            'last_reported_usage':usage,'event_errors':errors[-5:]})

def outcome(agent_exit,patch_bytes,test_exit,events):
    if agent_exit != 0: return 'failed','agent_failed'
    if events.get('event_errors'): return 'failed','agent_event_error'
    if not patch_bytes: return 'failed','no_changes'
    if test_exit != 0: return 'failed','tests_failed'
    return 'succeeded','verified_patch'

class Worker:
    def __init__(self,config: WorkerConfig,stop_requested=lambda:False):
        config.validate()
        self.config=config
        self.store=Store(config.root)
        self.redactor=Redactor.from_env_file(config.dsh_home/'.env')
        self.stop_requested=stop_requested

    def submit(self,requester,task,base_ref=None,*,request_key=None,origin=None,scope='code_change'):
        # 这里只是本地操作员 CLI 的白名单；远程身份认证在阶段三接入。
        if not self.config.is_allowed(requester): raise PermissionError('请求者不在开发管理员名单')
        if not isinstance(task,str) or not task.strip() or len(task.encode('utf-8'))>65536:
            raise ValueError('任务必须为非空文本且不超过 64 KiB')
        if request_key:
            existing=self.store.get_request(request_key)
            if existing: return existing
        ref=base_ref or self.config.base_ref
        commit=resolve_commit(self.config,ref)
        project_spec=project.pin(self.config)
        return self.store.submit({'requester':requester,'task':task,'base_ref':ref,
                                  'base_commit':commit,'test_command':self.config.test_command,
                                  'source_repo':str(self.config.source_repo.resolve()),
                                  'origin':origin or {},'scope':scope,'project':project_spec},request_key=request_key)

    def run_once(self):
        """持锁恢复中断状态，再原子领取一项；不自动重试。"""
        with exclusive_lock(self.config.lock_path):
            self.store.recover(project=self.redactor.value)
            source_release.reconcile(self.config,self.store)
            task=self.store.claim()
            if task is None: return None
            result=self.execute(task)
            candidate=result['result'].get('source_candidate')
            if (candidate and candidate['files'] and not candidate['core_approval_required']
                    and task['spec']['project'].get('auto_source_release')):
                try:
                    source_release.publish(self.config,self.store,task['id'])
                except Exception as exc:
                    self.store.source_publication(task['id'],{'state':'blocked',
                        'error':self.redactor.text(str(exc))[:300]})
            return self.store.get(task['id'])

    def execute(self,task):
        cfg=self.config
        task_id=task['id']
        folder=self.store.directory(task_id)
        # 会话后续轮次使用同一源码、venv 和 DSH home；本轮产物另存，保留历史证据。
        root_id=task['spec'].get('session_root',task_id)
        workspace_folder=self.store.directory(root_id)
        parent_id=task['spec'].get('continuation_of')
        started=time.monotonic()
        result={'base_commit':task['spec']['base_commit'],'agent_exit':None,'test_exit':None,
                'patch':None,'events':{},'durations':{},'artifacts':{}}
        status,reason='failed','worker_error'
        def cancelled():
            if self.stop_requested(): raise Stopped('worker_stopped')
            return self.store.get(task_id)['cancel_requested']
        interrupted=False
        try:
            if str(cfg.source_repo.resolve())!=task['spec']['source_repo']:
                raise ValueError('任务提交后源码仓库路径发生变化')
            for phase,operation in ([] if parent_id else [
                ('clone',lambda:prepare_clone(cfg,folder,task['spec']['base_commit'],cancelled)),
                ('environment',lambda:prepare_environment(cfg,folder,cancelled))]):
                self.store.phase(task_id,phase)
                before=time.monotonic(); operation()
                result['durations'][phase]=round(time.monotonic()-before,3)
            # 工作流候选以线上当前工作流为基线，补丁只含代理在此之上的改动。
            base=task['spec']['base_commit']
            project_spec=task['spec'].get('project')
            if project_spec and not parent_id:
                self.store.phase(task_id,'project')
                base=project.compose(cfg,workspace_folder,project_spec,cancelled)
                result['project_base_commit']=base
            publishing=task['spec'].get('scope')=='persistent_workflow' and cfg.auto_release
            session_id=None
            if parent_id:
                parent=self.store.get(parent_id)
                if parent['status'] not in ('succeeded','failed','timeout','cancelled'):
                    raise RuntimeError('前一开发轮次尚未结束')
                previous=event_summary(self.store.directory(parent_id)/'events.jsonl',self.redactor)
                session_id=previous['session_id']
                if not session_id or not (workspace_folder/'ws').is_dir():
                    raise RuntimeError('缺少可继续的 DSH 会话或工作副本，未重新执行原任务')
                root=self.store.get(root_id)
                base=root['result'].get('workflow_base_commit',root['result'].get('project_base_commit',base))
                if project_spec:
                    result['project_base_commit']=root['result'].get('project_base_commit',base)
                if publishing:
                    # 保留既有副本，不以同步线上目录覆盖代理产物；基线不一致时明确阻塞。
                    baseline=release.tree(cfg.live_workflows)
                    if release.tree(workspace_folder/'ws/workflows')!=baseline:
                        raise RuntimeError('工作流副本与线上不一致，需先协调候选与发布基线')
                result.update(session_root=root_id,continuation_of=parent_id)
            elif publishing:
                self.store.phase(task_id,'sync')
                baseline,base=release.sync_workspace(cfg,folder,cancelled)
                result['workflow_base_commit']=base
            self.store.phase(task_id,'agent')
            additions=self.store.inputs(task_id)
            prompt=task['spec']['task']+''.join('\n\n【协调者补充】\n'+item for item in additions)
            command=(agent_command(cfg,workspace_folder,prompt,session_id=session_id) if parent_id
                     else agent_command(cfg,workspace_folder,prompt))
            response=run_process(command,cwd=workspace_folder/'ws',
                env=agent_env(cfg,workspace_folder),stdout=folder/'events.jsonl',stderr=folder/'stderr.log',
                timeout=cfg.agent_timeout_sec,cancel=cancelled,max_bytes=cfg.max_log_bytes,
                idle_timeout=cfg.effective_agent_idle_timeout_sec)
            result['agent_exit']=response.exit_code
            result['durations']['agent']=round(response.seconds,3)
            result['events']=event_summary(folder/'events.jsonl',self.redactor)
            if session_id and result['events']['session_id']!=session_id:
                raise RuntimeError('DSH 未继续指定会话，停止收集与发布')
            self.store.phase(task_id,'collect')
            patch=collect_patch(cfg,workspace_folder,base,cancelled,artifact_folder=folder)
            result['patch']=patch
            if task['spec'].get('scope')=='persistent_workflow' and any(
                    not name.startswith(('workflows/','pytests/kkbk_engine/')) for name in patch['files']):
                raise RuntimeError('workflow_scope_violation')
            if patch['bytes']:
                self.store.phase(task_id,'test')
                test_cfg=replace(cfg,test_command=task['spec']['test_command'])
                response=run_process(test_command(test_cfg,workspace_folder),cwd=workspace_folder/'ws',env=agent_env(cfg,workspace_folder),
                    stdout=folder/'test.log',stderr=folder/'test.err',timeout=cfg.test_timeout_sec,
                    cancel=cancelled,max_bytes=cfg.max_log_bytes)
                result['test_exit']=response.exit_code
                result['durations']['test']=round(response.seconds,3)
                if response.exit_code == 0 and project_spec:
                    checks=[]
                    for index,check in enumerate(project_spec.get('checks',[])):
                        if not any(project.covers(name,check.get('when','.')) for name in patch['files']):
                            continue
                        from .workspace import sandbox_command
                        requires=check.get('requires',[])
                        if requires:
                            code='import importlib.util; names='+repr(requires)+'; missing=[n for n in names if importlib.util.find_spec(n) is None]; assert not missing, "缺少真实集成测试依赖："+repr(missing)'
                            command=sandbox_command(cfg,workspace_folder,[workspace_folder/'venv/bin/python','-c',code])
                            checked=run_process(command,cwd=workspace_folder/'ws',env=agent_env(cfg,workspace_folder),
                                stdout=folder/f'project-requires-{index}.log',stderr=folder/f'project-requires-{index}.err',
                                timeout=cfg.test_timeout_sec,cancel=cancelled,max_bytes=cfg.max_log_bytes)
                            if checked.exit_code:
                                checks.append({'index':index,'exit_code':checked.exit_code,'reason':'missing_test_dependencies'})
                                result['test_exit']=checked.exit_code
                                break
                        command=sandbox_command(cfg,workspace_folder,[workspace_folder/'venv/bin/python',*check['argv']])
                        checked=run_process(command,cwd=workspace_folder/'ws',env=agent_env(cfg,workspace_folder),
                            stdout=folder/f'project-check-{index}.log',stderr=folder/f'project-check-{index}.err',
                            timeout=cfg.test_timeout_sec,cancel=cancelled,max_bytes=cfg.max_log_bytes)
                        checks.append({'index':index,'exit_code':checked.exit_code})
                        if checked.exit_code:
                            result['test_exit']=checked.exit_code
                            break
                    result['project_checks']=checks
                # 测试产生的实际源码变化也必须披露，不能把测试前补丁当作最终版本。
                after=collect_patch(cfg,workspace_folder,base,cancelled,artifact_folder=folder)
                result['patch']=after
                if after['sha256']!=patch['sha256']:
                    raise RuntimeError('workspace_changed_during_tests')
            status,reason=outcome(result['agent_exit'],patch['bytes'],result['test_exit'],result['events'])
            if project_spec and status=='succeeded' and not publishing:
                try:
                    result['source_candidate']=source_release.save_candidate(cfg,task,workspace_folder/'ws',result)
                    # 依赖变化时另封完整源码，供版本化依赖环境试运行使用。
                    if source_release.needs_dependencies(project_spec,patch['files']):
                        candidate_folder=folder/'source-candidate'
                        source_release.seal_tree(cfg,candidate_folder,workspace_folder/'ws',cancelled)
                except (source_release.SourceReleaseError,ValueError) as exc:
                    # 开发验证与发布范围分别报告，不能把缺少映射说成代理开发失败。
                    result['source_candidate']={'state':'blocked','files':0,'core_approval_required':False,
                        'error':self.redactor.text(str(exc))[:300]}
            # 发布结果单独记录：候选本身已验证，发布失败不改写任务结论。
            if publishing and status=='succeeded':
                self.store.phase(task_id,'release')
                result['release']=release.publish(cfg,task_id=task_id,workspace=workspace_folder/'ws',
                                                  files=result['patch']['files'],baseline=baseline)
        except Stopped as exc:
            reason=exc.reason
            status='cancelled' if reason in ('cancelled','worker_stopped') else 'timeout' if reason=='timeout' else 'failed'
        except KeyboardInterrupt:
            status,reason,interrupted='cancelled','worker_stopped',True
        except Exception as exc:
            # 仅记录异常类型与受控消息；原始工具日志留在私有开发目录。
            result['error']=type(exc).__name__+': '+self.redactor.text(str(exc))[:512]
            status,reason='failed','worker_error'
        finally:
            result['durations']['total']=round(time.monotonic()-started,3)
            for name in ('events.jsonl','stderr.log','patch.diff','test.log','test.err'):
                if (folder/name).exists(): result['artifacts'][name]=name
            self.store.finish(task_id,status,reason,self.redactor.value(result))
            completed=self.store.get(task_id)
            atomic_json(folder/'summary.json',self.redactor.value(completed))
        if interrupted: raise KeyboardInterrupt
        return completed

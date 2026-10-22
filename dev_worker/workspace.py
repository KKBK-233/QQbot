"""独立源码副本、开发环境与 Linux 文件隔离，不加载生产配置。"""
from pathlib import Path
import hashlib
import json
import os
import re
import shlex
import shutil
import subprocess
import tempfile
import threading

from .process import run_process

TASK_VENV_MOUNT = '.dev-worker-venv'
# dsh-llm-deepseek 的 DEFAULT_CONTEXT_WINDOW；压缩阈值按它换算成比例。
DEEPSEEK_CONTEXT_WINDOW = 1_000_000
_GIT_CONFIG_LOCK = threading.Lock()


def visible_venv(config, folder):
    """DSH 内层仅允许工作区写入，提供同一任务 venv 的工作区内挂载入口。"""
    return folder/'ws'/TASK_VENV_MOUNT if config.use_sandbox else folder/'venv'

def host_env(config):
    """Git 只使用 worker 专用配置，不继承凭据或影响用户全局 Git。"""
    config.root.mkdir(parents=True,exist_ok=True,mode=0o700)
    home=config.root/'host-home'
    home.mkdir(exist_ok=True,mode=0o700)
    text='[user]\n name = kkbk-dev-worker\n email = worker@localhost\n[safe]\n'
    gitconfig=config.gitconfig_path.with_name('gitconfig-'+hashlib.sha256(str(config.source_repo.resolve()).encode()).hexdigest()[:12])
    for path in (config.source_repo.resolve(),config.source_repo.resolve()/'.git'):
        text+=' directory = '+json.dumps(str(path))+'\n'
    # 多个控制请求可并发解析提交，不能在 Git 读取时截断共用配置。
    with _GIT_CONFIG_LOCK:
        if not gitconfig.exists() or gitconfig.read_text(encoding='utf-8')!=text:
            fd,temporary=tempfile.mkstemp(prefix='.gitconfig-',dir=config.root)
            try:
                with os.fdopen(fd,'w',encoding='utf-8') as stream: stream.write(text)
                os.replace(temporary,gitconfig)
            finally:
                Path(temporary).unlink(missing_ok=True)
    env={'PATH':os.environ.get('PATH','') if os.name=='nt' else config.agent_path,
         'HOME':str(home),'LANG':'C.UTF-8','GIT_CONFIG_GLOBAL':str(gitconfig),
         'GIT_CONFIG_NOSYSTEM':'1','GIT_TERMINAL_PROMPT':'0','GIT_LFS_SKIP_SMUDGE':'1'}
    if os.name=='nt':
        for name in ('SYSTEMROOT','WINDIR','TEMP','TMP'):
            if name in os.environ: env[name]=os.environ[name]
    return env

def resolve_commit(config, ref=None):
    result=subprocess.run(['git','-C',str(config.source_repo),'rev-parse','--verify','--end-of-options',
                           (ref or config.base_ref)+'^{commit}'],env=host_env(config),capture_output=True,
                          timeout=15,check=True)
    commit=result.stdout.decode().strip()
    if len(commit)!=40 or any(c not in '0123456789abcdef' for c in commit):
        raise ValueError('无法解析固定源码提交')
    return commit

def prepare_clone(config, folder, commit, cancel):
    """仅克隆已提交内容，不复制源仓库的配置、数据或未提交修改。"""
    workspace=folder/'ws'
    if workspace.exists(): raise ValueError('工作副本已存在，不覆盖')
    env=host_env(config)
    commands=[['git','clone','--quiet','--no-hardlinks','--no-checkout',str(config.source_repo),str(workspace)],
              ['git','-C',str(workspace),'checkout','--quiet','--detach',commit],
              ['git','-C',str(workspace),'remote','remove','origin']]
    for index,command in enumerate(commands):
        result=run_process(command,cwd=config.root,env=env,stdout=folder/f'prepare-{index}.log',
            stderr=folder/f'prepare-{index}.err',timeout=config.prepare_timeout_sec,cancel=cancel,
            max_bytes=config.max_log_bytes)
        if result.exit_code: raise RuntimeError(f'clone_step_{index}_failed')
    return workspace

def setup_base(config, full=False):
    """显式准备开发测试依赖；默认只装 pytest，不复制生产虚拟环境。"""
    env=host_env(config)
    commands=[]
    if not config.base_python.exists():
        commands.append([config.system_python,'-m','venv',str(config.base_venv)])
    install=[str(config.base_python),'-m','pip','install','--disable-pip-version-check','-i',config.pip_index_url,
             'pytest','pytest-asyncio']
    if full: install+=['-r',str(config.requirements_path())]
    commands.append(install)
    for index,command in enumerate(commands):
        result=run_process(command,cwd=config.root,env=env,stdout=config.root/f'setup-{index}.log',
            stderr=config.root/f'setup-{index}.err',timeout=config.prepare_timeout_sec,
            max_bytes=config.max_log_bytes)
        if result.exit_code: raise RuntimeError(f'setup_step_{index}_failed，检查开发基座日志')
    return {'base_python':str(config.base_python),'full_requirements':full}

def prepare_environment(config, folder, cancel):
    """任务 venv 可写，基础包只读；代理不取得账本或产物目录的写权限。"""
    if not config.base_python.exists(): raise RuntimeError('请先执行 dev_worker setup')
    venv=folder/'venv'
    result=run_process([config.system_python,'-m','venv',str(venv)],cwd=folder,env=host_env(config),
        stdout=folder/'venv.log',stderr=folder/'venv.err',timeout=config.prepare_timeout_sec,cancel=cancel)
    if result.exit_code: raise RuntimeError('task_venv_failed')
    base_sites=list((config.base_venv/'lib').glob('python*/site-packages'))
    task_sites=list((venv/'lib').glob('python*/site-packages'))
    if len(base_sites)!=1 or len(task_sites)!=1: raise RuntimeError('开发 venv 目录结构异常')
    (task_sites[0]/'base.pth').write_text(str(base_sites[0])+'\n')
    if config.use_sandbox:
        mount=visible_venv(config,folder)
        if mount.exists() or mount.is_symlink(): raise RuntimeError('源码占用了任务 venv 保留路径')
        mount.mkdir()
        # pip 等入口脚本和 activate 不能再指向内层只读的宿主路径。
        for script in (venv/'bin').iterdir():
            if script.is_symlink() or not script.is_file(): continue
            raw=script.read_bytes()
            script.write_bytes(raw.replace(str(venv).encode(),str(mount).encode()))
        # 目录仅作为挂载入口；真实依赖在工作树外，不能进入候选补丁。
        exclude=folder/'ws/.git/info/exclude'
        with exclude.open('a',encoding='utf-8') as stream:
            stream.write('\n/'+TASK_VENV_MOUNT+'/\n')
    home=folder/'agent-home'
    profile=home/'profiles/headless'
    profile.mkdir(parents=True,mode=0o700)
    source=config.dsh_home/'profiles/headless'
    for name in ('package.json','cordis.yml','cordis.patch.yml','pnpm-workspace.yaml'):
        path=source/name
        if path.exists(): shutil.copyfile(path,profile/name)
    if not (profile/'package.json').is_file(): raise RuntimeError('缺少已准备的 DSH headless profile')
    for name in ('sessions','storages'):
        (home/name).mkdir(mode=0o700)
    (home/'.env').touch(mode=0o600)

def write_dsh_patch(config, folder):
    """每轮启动前按当前配置重写；续聊旧会话不能沿用开工时的旧覆盖层。"""
    patch=folder/'dsh-patch.yml'
    # DeepSeek 声明 1M 窗口，默认 80% 才压缩，长会话每步重带几十万旧上下文；按绝对阈值提前压缩。
    ratio=config.compact_threshold_tokens/DEEPSEEK_CONTEXT_WINDOW
    patch.write_text(f'- id: bash-sandbox\n  config:\n    timeoutMs: {config.bash_timeout_ms}\n'
                     f'- id: compaction-basic\n  config:\n    thresholdRatio: {ratio:.4f}\n'
                     f'    retainTokens: {config.compact_retain_tokens}\n    maxTokens: 8192\n',encoding='utf-8')
    return patch

def agent_env(config,folder):
    """最小环境不导出空密钥，DSH 在隔离环境读取专用凭据文件。"""
    venv=visible_venv(config,folder)
    env={'PATH':str(venv/'bin')+os.pathsep+config.agent_path,
            'HOME':str(folder/'agent-home'),'DSH_HOME':str(folder/'agent-home'),
            'DSH_TELEMETRY_DISABLED':'1','LANG':'C.UTF-8','VIRTUAL_ENV':str(venv),
            'PIP_CACHE_DIR':str(venv/'.cache/pip'),
            'PYTHONDONTWRITEBYTECODE':'1','GIT_TERMINAL_PROMPT':'0','GIT_CONFIG_NOSYSTEM':'1'}
    if config.project_manifest:
        # 根仓维护代码位于组合副本中，不引用 Windows 原始仓或生产源码。
        env['PYTHONPATH']=os.pathsep.join([str(folder/'ws'),str(folder/'ws/.project/integration')])
    if os.name=='nt':
        for name in ('SYSTEMROOT','WINDIR','TEMP','TMP'):
            if name in os.environ: env[name]=os.environ[name]
    return env

def sandbox_command(config,folder,command,*,agent=False):
    """开发阶段可联网；测试/补丁收集断网，生产及 Windows 挂载目录均隐藏。"""
    if not config.use_sandbox:
        return [str(value) for value in command]
    if not Path('/usr/bin/bwrap').exists(): raise RuntimeError('缺少 bwrap，不降级到宿主执行')
    if (folder/'ws/.git').is_symlink(): raise RuntimeError('拒绝符号链接 Git 目录')
    args=['/usr/bin/bwrap','--ro-bind','/','/','--dev','/dev','--proc','/proc',
          '--tmpfs','/home','--tmpfs','/root','--tmpfs','/mnt','--tmpfs','/run','--tmpfs','/tmp',
          '--unshare-pid','--unshare-ipc','--unshare-uts','--die-with-parent','--new-session',
          '--cap-drop','ALL','--bind',str(folder/'ws'),str(folder/'ws'),
          '--bind',str(folder/'venv'),str(folder/'venv'),
          '--bind',str(folder/'venv'),str(visible_venv(config,folder)),
          '--ro-bind',str(config.base_venv),str(config.base_venv),
          '--bind',str(folder/'agent-home'),str(folder/'agent-home')]
    if agent:
        if not (config.dsh_home/'.env').is_file(): raise RuntimeError('DSH 专用凭据文件未准备')
        # DSH 启动时会重写 cordis.yml，因此只给它任务私有 profile，绝不映射全局 profile 可写。
        args+=['--ro-bind',str(config.dsh_root),str(config.dsh_root),
               '--ro-bind',str(config.dsh_home/'.env'),str(folder/'agent-home/.env'),
               '--ro-bind',str(folder/'dsh-patch.yml'),str(folder/'dsh-patch.yml')]
    else:
        args+=['--unshare-net']
    args+=['--chdir',str(folder/'ws'),'--',*[str(value) for value in command]]
    return args

def developer_prompt(task, stall_limit):
    return ('请在当前独立工作副本完成开发任务。可以读取和修改整个项目、运行命令与测试。'
            '开发范围包括人设、system 与任务提示词、工作流、插件、依赖和执行代码。'
            '组合副本的主仓在当前目录，维护根仓在 .project/integration，独立插件已展开；'
            '.project-context.json 是当前人设的脱敏参考，不是配置发布入口。'
            '源码发布由宿主按策略封装和执行，以 source_release 回执为准，不能自行探查部署通道。'
            '需要新增执行能力时补齐代码与接线，不用一份文字规则代替实现。'
            '在授权目标内持续修复并逐项验收；缺少信息时明确提出问题，等待协调者在同一会话补充。'
            '只处理本任务，不访问生产、其他工作区或凭据，不提交、推送或部署。'
            '测试使用当前 VIRTUAL_ENV 的 Python。最后说明改动、测试和未完成事项。'
            # 实测续跑轮次大量步骤花在探查端口、socket 和重读已读代码上，每步都重带全部上下文。
            '控制步数：先定位再修改，不重复读取已读过且未改动的文件，测试先跑目标文件再跑全量。'
            '任务所需的接口、服务或凭据在工作副本内找不到时，直接在答复中说明缺什么并结束本轮，'
            '不要扫描端口、进程、socket 或开发目录去寻找替代入口。'
            # 实测外部站点被 Cloudflare 拦截后，代理连续十余步用搜索、存档站、转抓服务和别的语言实现绕路。
            '外部资源不可用（被拦截、断网、缺 key、限流）时，直接说明阻塞点和需要协调者提供的输入并结束本轮；'
            '不要用网页搜索、代理、存档站、第三方转抓服务或其他语言的实现绕过，也不要伪造“真实样本”。'
            '验收条件在当前环境做不到时，先指出冲突再停下，不要默默降级或反复尝试。'
            f'连续 {stall_limit} 步既没有修改文件也没有运行测试，宿主会终止本轮；'
            '卡住时应主动总结卡点并结束，而不是继续探索。\n\n'+task)

def agent_command(config,folder,task,session_id=None):
    command=[config.dsh_bin,'--profile','headless','--patch',folder/'dsh-patch.yml',
             '--json']
    if session_id:
        # 使用实际安装的 headless 会话接口；标识只从持久事件读取，不接收模型指定。
        if not isinstance(session_id,str) or not re.fullmatch(r'session-[A-Za-z0-9-]{1,100}', session_id):
            raise ValueError('DSH 会话标识无效')
        command += ['--session-id',session_id]
    command += [developer_prompt(task,config.max_unproductive_steps)]
    return sandbox_command(config,folder,command,agent=True)

def collect_patch(config,folder,base_commit,cancel,artifact_folder=None):
    """Git 在断网隔离环境执行，防止仓库过滤器在宿主执行；包含新增文件。"""
    output_folder=artifact_folder or folder
    env=agent_env(config,folder)
    git='/usr/bin/git' if config.use_sandbox else 'git'
    prefix=[git,'-c','core.hooksPath=/dev/null','-c','core.fsmonitor=false']
    if (folder/'project.json').is_file():
        spec=json.loads((folder/'project.json').read_text())['spec']
        # 独立插件目录被主仓忽略，显式纳入这些组件中的新增源码。
        paths=[item['mount'] for item in spec['components']]
        if (folder/'ws/plugins').is_dir(): paths.append('plugins')
        if paths:
            command=prefix+['add','-A','-f','--',*paths,
                            ':(exclude)**/__pycache__/**',':(exclude)**/.pytest_cache/**']
            result=run_process(sandbox_command(config,folder,command),cwd=folder/'ws',env=env,
                stdout=output_folder/'component-stage.log',stderr=output_folder/'component-stage.err',
                timeout=config.prepare_timeout_sec,cancel=cancel,max_bytes=config.max_log_bytes)
            if result.exit_code: raise RuntimeError('component_stage_failed')
    for label,command in [('stage',prefix+['add','-A','--','.']),
                           ('patch',prefix+['diff','--cached','--binary','--no-ext-diff','--no-textconv',base_commit,'--','.']),
                           ('files',prefix+['diff','--cached','--name-only','-z',base_commit,'--','.'])]:
        output=output_folder/('patch.diff' if label=='patch' else label+'.log')
        result=run_process(sandbox_command(config,folder,command),cwd=folder/'ws',env=env,
            stdout=output,stderr=output_folder/(label+'.err'),timeout=config.prepare_timeout_sec,cancel=cancel,
            max_bytes=config.max_patch_bytes if label=='patch' else config.max_log_bytes)
        if result.exit_code: raise RuntimeError(label+'_failed')
    patch=(output_folder/'patch.diff').read_bytes()
    names=(output_folder/'files.log').read_bytes().decode('utf-8').split('\0')
    return {'sha256':hashlib.sha256(patch).hexdigest(),'bytes':len(patch),
            'files':[name for name in names if name]}

def test_command(config,folder):
    # 命令来自维护者配置而非模型输出或任务正文；不使用 shell=True。
    return sandbox_command(config,folder,[folder/'venv/bin/python',*shlex.split(config.test_command)])

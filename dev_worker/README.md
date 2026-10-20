# 独立开发 worker（阶段 2）

将本地维护者提交的开发任务变成独立源码副本、真实开发代理执行、补丁及独立测试结果。当前使用已有的 DeepSeek 官方 DSH `0.2.1-rc.2`，没有改用 Codex 或 Claude 后端。

当前支持本机 CLI/worker 及阶段 3 的 Unix socket 协调入口；QQ 宿主绑定实际发送者后可提交、查询和取消开发任务。不监听 HTTP，不部署生成代码；阶段 4 起，持久工作流候选通过测试后自动发布到线上 `workflows/`，带备份、加载自检、自动恢复和回退（`release.py`）。群内用法和回执边界见 `docs/kkbk-engine/development-coordination.md`。

## 前提

- WSL/Linux，Python 3.12、Git、venv、Bubblewrap 与可用的 Node.js。
- 已安装的 DSH：`/home/qqbot/dev-harness/dsh/node_modules/.bin/dsh`。
- 已准备的 headless profile：`/home/qqbot/dev-harness/home/profiles/headless/`。
- DSH 专用 `.env` 位于 `/home/qqbot/dev-harness/home/.env`；由本地维护者准备，worker 不复制 MaiBot 模型配置或 QQ 登录目录。

DSH 仍处于预览期，已按本机安装版本的 CLI 和事件协议验证。升级后应重新验证 `--profile headless --json`、profile 组合及 `bash-sandbox.timeoutMs`。

## 操作

以下命令从 Windows PowerShell 调用；可在 WSL 中设置 `PYTHONPATH` 后直接运行模块。

```powershell
# 显式创建独立基础 venv，默认仅安装 pytest/pytest-asyncio。
wsl -d Ubuntu -u qqbot -- env PYTHONPATH=/mnt/d/code/web/QQbot python3 -B -m dev_worker setup

# task.md 是维护者创建的 UTF-8 开发需求文件。
wsl -d Ubuntu -u qqbot -- env PYTHONPATH=/mnt/d/code/web/QQbot python3 -B -m dev_worker submit --requester 1020050112 --task-file /mnt/d/code/web/QQbot/task.md

# 执行最早的一项任务，或用 --loop 持续轮询。
wsl -d Ubuntu -u qqbot -- env PYTHONPATH=/mnt/d/code/web/QQbot python3 -B -m dev_worker run --once

# 常驻协调服务（真实运行由 Supervisor dev-worker 管理）。
wsl -d Ubuntu -u qqbot -- env PYTHONPATH=/mnt/d/code/web/QQbot python3 -B -m dev_worker serve

# 省略编号列出最近任务；指定编号查看状态和结果。
wsl -d Ubuntu -u qqbot -- env PYTHONPATH=/mnt/d/code/web/QQbot python3 -B -m dev_worker status
wsl -d Ubuntu -u qqbot -- env PYTHONPATH=/mnt/d/code/web/QQbot python3 -B -m dev_worker status TASK_ID
wsl -d Ubuntu -u qqbot -- env PYTHONPATH=/mnt/d/code/web/QQbot python3 -B -m dev_worker cancel TASK_ID
```

`setup --full` 才安装源码仓 requirements.txt 中的完整开发依赖。默认引擎纯测试不需要生产全量依赖。任务也可以通过自己的 venv 安装所需依赖，基础 venv 对代理只读。

任务 venv 实体位于工作树外，通过工作区内 `.dev-worker-venv/` 挂载供 DSH 使用，`PATH`、`VIRTUAL_ENV` 与 pip 缓存均指向该入口。这样内层仍保持 `workspace-write`，无需放宽到全文件系统写入。挂载入口由 Git 本地 exclude 排除，依赖不进入补丁；源码不能占用这个保留目录。

`--requester` 是本地可信操作员提供的审计字段和白名单检查，不构成远程认证。不能把这个 CLI 参数直接暴露给模型或群友，未来 QQ 调用必须由宿主绑定实际发送者。

CLI 输出 JSON；`run --once` 的任务失败/超时/取消返回退出码 2，命令或配置错误为 1，进程停止为 130，成功或无待办为 0。查询一个失败任务的 status 命令仍返回 0，因为查询本身成功。

## 配置

使用 `KKBK_DEV_WORKER_` 前缀的环境变量；配置错误直接报错。

| 后缀 | 默认值或用途 |
| --- | --- |
| `ROOT` | `/home/qqbot/dev-harness`，独立账本、任务、基础 venv |
| `SOURCE_REPO` | `/mnt/d/code/web/QQbot/MaiBot`，只读克隆来源 |
| `BASE_REF` | `codex/kkbk-engine-integration`，提交任务时固定为 commit |
| `TEST_COMMAND` | `-m pytest -q --noconftest -p no:cacheprovider pytests/kkbk_engine`，作为 Python 参数执行 |
| `ALLOWED_REQUESTERS` | `1020050112`，多个以逗号分隔 |
| `AGENT_TIMEOUT` / `AGENT_IDLE_TIMEOUT` | 总期限 1800 秒；无输出配置值 300 秒，实际至少为 bash 上限向上取整后加 30 秒，默认 930 秒 |
| `TEST_TIMEOUT` / `PREPARE_TIMEOUT` | 1800 秒 / 600 秒 |
| `BASH_TIMEOUT_MS` | 900000，DSH 单工具超时仍受总代理期限约束 |
| `MAX_LOG_BYTES` / `MAX_PATCH_BYTES` | 每阶段输出合计 8 MiB / 补丁 4 MiB |
| `PIP_INDEX` | 阿里云 PyPI 镜像，可由维护者更改 |
| `SANDBOX` | 默认 true；真实 CLI run 不接受关闭隔离 |
| `LIVE_WORKFLOWS` / `LIVE_LOADER` | `/home/qqbot/QQbot/MaiBot/workflows` / 同仓 `src/kkbk_engine/workflows.py`；任一设为 `off` 关闭工作流自动发布 |

当前每项任务针对一个完整的已提交 Git 仓。默认是 MaiBot；选择根集成仓或独立插件仓时，需要同时配置对应测试命令。尚未自动组合多个嵌套仓，也不复制任何未提交补丁。因此工作副本不是生产实例的逐字镜像。

无输出保护按 stdout/stderr 文件增长计时，不将静默的工具运行直接视为卡住。实际空闲期限取 `max(AGENT_IDLE_TIMEOUT, ceil(BASH_TIMEOUT_MS / 1000) + 30)`；总任务期限仍优先生效。当前本地路径克隆可按固定 SHA 检出只由源仓远端跟踪引用持有的提交，不要求它出现在克隆后的分支列表中。

## 状态与产物

`worker.sqlite3` 是状态唯一来源，SQLite 事务处理提交、领取、取消及终态竞争。`tasks/<id>/summary.json` 是完成后的原子写入结果快照。

状态：`queued → running → succeeded / failed / timeout / cancelled`。

- 单 worker 文件锁防止同时执行；锁随进程退出释放。
- 取得锁后将遗留 running 标记为 `worker_interrupted`，不会自动重跑可能已产生副作用的任务。
- queued 取消立即生效，running 取消由当前执行器终止进程组。
- 代理失败、输出错误事件、没有补丁、独立测试失败分别记录，不以 final 文本作为成功证据。
- 成功仅表示有补丁且配置的测试进程通过；不表示已发布、业务效果已验收或测试本身不可被修改。
- 记录测试前后补丁摘要；测试若改动实际源码，本轮拒绝当作已验证版本。

每任务目录包含 `ws/`、任务 venv、私有 DSH home、`events.jsonl`、`stderr.log`、`patch.diff`、`test.log`、`test.err` 和 `summary.json`。原始日志、任务正文和模型思考均属于私有开发资料，不自动上传 GitHub。用量只记录最后一次供应商报告值，不将累计与单轮字段混加，也不推算账单。

结果字段、完成/恢复快照和 CLI 展示会脱敏：读取专用 `.env` 与宿主敏感环境字段中的已知原值，并遮盖常见令牌前缀、认证头、敏感赋值、URL 用户密码及 PEM 私钥。先脱敏再截断，数值用量保留。私有 SQLite 中的任务原文及原始日志不重写；该机制不能保证发现任意编码、分段或未知格式的凭据，脱敏摘要也不自动成为可公开资料。

## 执行边界

- 只克隆固定提交；不复制源仓的未提交配置、数据库或登录资料。克隆后删除 origin，避免误推源仓或上游。
- 代理阶段隐藏 `/home`、`/root`、`/mnt`、`/run`、`/tmp` 的原挂载，仅回填任务工作区、任务 venv、任务私有 home，以及只读 DSH 安装和基础 venv。
- 任务账本、summary、补丁与宿主日志不映射进代理；它只能写当前工作副本及自己的开发环境。
- DSH 专用凭据只读挂载到私有 home，不复制进任务目录；DSH 本身需要读取它。该凭据并非对同一代理进程中的工具完全不可见，不应给予超过此开发用途的权限。
- 代理可联网以访问模型和安装依赖；这是共享主机网络，不是出站域名防火墙。独立复测和 Git 补丁收集使用断网命名空间，不挂载 DSH 凭据。
- Git add/diff 也在隔离环境运行，避免代理修改的 Git 过滤器在宿主执行。补丁包含未忽略的新增文件，已有提交也按固定基准比较。
- 进程使用独立进程组；取消/超时终止组，Bubblewrap 的 PID 命名空间和父进程退出行为负责清理内部子孙进程。
- 源码补丁不自动应用；工作流候选只由宿主侧 worker（不在代理沙箱内）写入线上 `workflows/`。不调用生产管理接口，不重启服务，不发送 QQ 消息。文件隔离与 WSL 共用内核，不是虚拟机。

## 验证记录

- 新增 worker 回归：WSL 17 项通过，包括真实命名空间的隐藏路径、断网和超时子进程清理。
- 真实任务 `20261020T030206Z-58fbda99917e`，固定基准 `cdbfca22bcd1b70097267a9d328461458359ab0c`。
- DSH 真实调用 bash/write，在副本中只新增 `docs/dev-worker-smoke.md`；代理退出 0，worker 独立复测退出 0。
- 独立结果为 83 passed、1 skipped；跳过项依赖未复制的私有群聊回放数据。
- 补丁 209 字节，`git apply --check` 通过；任务前后集成、源仓和生产仓的 Git 状态指纹相同。
- 没有将试验补丁应用到源码或生产。当时 QQ 教学和自动发布尚未接通；当前阶段 3 进度另见开发协调文档。

### 审阅后修复与加强验收

- 修复静默命令可能被提前结束的问题；实际无输出期限覆盖 bash 上限，并留 30 秒事件返回余量，总墙钟期限不变。
- 新增统一结果脱敏，覆盖 CLI、错误、模型事件摘要和完成/恢复快照；原始任务和日志仍私有保留。
- 第一次加强验收 `20261020T032825Z-b64919d2d8a9` 已完成代码修复及独立测试（85 passed/1 skipped），但依赖安装因 DSH 内层把任务 venv 视为工作区外而失败；不能将该次任务状态 succeeded 当作依赖安装验收通过。
- 补充工作区内的任务 venv 挂载后，真实任务 `20261020T033146Z-db2418ff6917` 完成已有工作流加载代码修改与测试补充：先实际观察 4 个测试失败，修复后 worker 独立复测 87 passed/1 skipped。
- `packaging==24.2` 成功安装到该任务 venv；另在断网环境独立验证版本、导入路径及 pip 脚本入口。基础 venv 保持只读。
- 候选补丁 4294 字节，只改工作流加载器和对应测试；`git apply --check` 通过，集成、源仓及生产仓状态指纹保持一致，补丁未应用。
- 当前 WSL worker 回归 25 项通过；Windows 根测试运行 38 项，34 项通过、4 项 Linux 专用测试跳过。另覆盖仅远端跟踪引用持有的提交克隆、凭据跨截断边界、恢复快照脱敏及任务 venv 写入边界。
- 以上不代表 QQ 教学派发或自动发布已接入；取消和超时仍由自动化进程/命名空间测试验收，未新增真实模型取消演练。

### 阶段 3 协调入口验收

- 管理员实际来源绑定、持久教学范围、并发重复提交去重、跨会话拒绝、取消、重启恢复、通知去重及确认协议均有回归。
- WSL 开发执行及协调测试共 38 项通过；Windows 根测试 51 项，46 项通过、5 项 Linux 专用测试跳过；当前源码引擎测试 87 项通过。
- 真实 QQ 教学任务产出工作流候选与测试，独立复测 88 passed/1 skipped，取得真实发送回执。实测修正的范围与确认接线问题见 harness-plan.md。
- 该规则仅用于验收，候选和补丁已按用户要求清理，相关单条测试记忆通过原生接口撤回；现行工作流版本保持 1.0.0，任务查询显示候选已废弃。

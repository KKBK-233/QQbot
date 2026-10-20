# 独立开发 worker（阶段 2）

将本地维护者提交的开发任务变成独立源码副本、真实开发代理执行、补丁及独立测试结果。当前使用已有的 DeepSeek 官方 DSH `0.2.1-rc.2`，没有改用 Codex 或 Claude 后端。

当前是本机 CLI/worker，不监听 HTTP，不注册 QQ 工具，不部署生成代码。阶段 3 再接真实 QQ 身份和任务派发，阶段 4 再接发布与恢复。

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

# 省略编号列出最近任务；指定编号查看状态和结果。
wsl -d Ubuntu -u qqbot -- env PYTHONPATH=/mnt/d/code/web/QQbot python3 -B -m dev_worker status
wsl -d Ubuntu -u qqbot -- env PYTHONPATH=/mnt/d/code/web/QQbot python3 -B -m dev_worker status TASK_ID
wsl -d Ubuntu -u qqbot -- env PYTHONPATH=/mnt/d/code/web/QQbot python3 -B -m dev_worker cancel TASK_ID
```

`setup --full` 才安装源码仓 requirements.txt 中的完整开发依赖。默认引擎纯测试不需要生产全量依赖。任务也可以通过自己的 venv 安装所需依赖，基础 venv 对代理只读。

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
| `AGENT_TIMEOUT` / `AGENT_IDLE_TIMEOUT` | 1800 秒 / 300 秒无事件输出 |
| `TEST_TIMEOUT` / `PREPARE_TIMEOUT` | 1800 秒 / 600 秒 |
| `BASH_TIMEOUT_MS` | 900000，DSH 单工具超时仍受总代理期限约束 |
| `MAX_LOG_BYTES` / `MAX_PATCH_BYTES` | 每阶段输出合计 8 MiB / 补丁 4 MiB |
| `PIP_INDEX` | 阿里云 PyPI 镜像，可由维护者更改 |
| `SANDBOX` | 默认 true；真实 CLI run 不接受关闭隔离 |

当前每项任务针对一个完整的已提交 Git 仓。默认是 MaiBot；选择根集成仓或独立插件仓时，需要同时配置对应测试命令。尚未自动组合多个嵌套仓，也不复制任何未提交补丁。因此工作副本不是生产实例的逐字镜像。

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

## 执行边界

- 只克隆固定提交；不复制源仓的未提交配置、数据库或登录资料。克隆后删除 origin，避免误推源仓或上游。
- 代理阶段隐藏 `/home`、`/root`、`/mnt`、`/run`、`/tmp` 的原挂载，仅回填任务工作区、任务 venv、任务私有 home，以及只读 DSH 安装和基础 venv。
- 任务账本、summary、补丁与宿主日志不映射进代理；它只能写当前工作副本及自己的开发环境。
- DSH 专用凭据只读挂载到私有 home，不复制进任务目录；DSH 本身需要读取它。该凭据并非对同一代理进程中的工具完全不可见，不应给予超过此开发用途的权限。
- 代理可联网以访问模型和安装依赖；这是共享主机网络，不是出站域名防火墙。独立复测和 Git 补丁收集使用断网命名空间，不挂载 DSH 凭据。
- Git add/diff 也在隔离环境运行，避免代理修改的 Git 过滤器在宿主执行。补丁包含未忽略的新增文件，已有提交也按固定基准比较。
- 进程使用独立进程组；取消/超时终止组，Bubblewrap 的 PID 命名空间和父进程退出行为负责清理内部子孙进程。
- 不自动应用补丁，不调用生产管理接口，不重启服务，不发送 QQ 消息。文件隔离与 WSL 共用内核，不是虚拟机。

## 验证记录

- 新增 worker 回归：WSL 17 项通过，包括真实命名空间的隐藏路径、断网和超时子进程清理。
- 真实任务 `20261020T030206Z-58fbda99917e`，固定基准 `cdbfca22bcd1b70097267a9d328461458359ab0c`。
- DSH 真实调用 bash/write，在副本中只新增 `docs/dev-worker-smoke.md`；代理退出 0，worker 独立复测退出 0。
- 独立结果为 83 passed、1 skipped；跳过项依赖未复制的私有群聊回放数据。
- 补丁 209 字节，`git apply --check` 通过；任务前后集成、源仓和生产仓的 Git 状态指纹相同。
- 没有将试验补丁应用到源码或生产。完整 QQ 教学和自动发布仍属阶段 3/4。

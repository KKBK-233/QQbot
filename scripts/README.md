# 运维与部署脚本

根目录保留被启动脚本或 Supervisor 固定路径引用的运行组件，以及暂未移动的安装/配置入口。历史个案探针不属于当前验收入口。

独立开发执行入口为 `python -m dev_worker`，不属于生产部署脚本。setup、submit、run、serve、status、cancel 用法见 `dev_worker/README.md`；源码补丁不自动应用；工作流候选的自动发布与回退见 `docs/kkbk-engine/development-coordination.md`。

## checks：核验与诊断

| 脚本 | 用途 | 执行范围 |
| --- | --- | --- |
| `check-wsl.py` | 管理鉴权、QQ 登录状态 | 本机只读，不发送消息 |
| `verify-sandbox-retired.py` | 确认旧沙箱插件及特权入口已撤销 | 本机只读，不恢复旧组件 |
| `verify-kkbk-engine.py` | 引擎协议、补试、工具去重及身份绑定 | 默认模拟；`--live` 调用真实模型，不发群消息 |
| `verify-development.py` | 管理员工具、宿主身份和 socket 接线 | `--stage` 检查暂存源码；默认检查线上，不提交或发送消息 |
| `verify-task-assist.py` | 任务识图插件验收 | 部分模式会调用真实模型，先读参数 |
| `verify-search-reply.py` | 搜索插件与回复桥接 | 先读参数与文件头说明 |
| `verify-webpage.py` | 网页读取插件验收 | 先读参数与文件头说明 |
| `verify-image-network.py` | 生图链路网络和鉴权诊断 | 联网；不提交生成任务 |
| `verify-deepseek-manual.py` | 手动模型/平台核验 | 执行前核对参数与副作用 |
| `diagnose-deepseek-timeout.py` | 请求超时诊断 | 依赖实际环境，非日常单元测试 |
| `verify-mention-priority.py` | 旧调度器兼容验证 | 其他会话仍可走旧引擎，暂保留 |

WSL 示例：

```powershell
wsl -d Ubuntu -u qqbot -- python3 /mnt/d/code/web/QQbot/scripts/checks/check-wsl.py
```

## deploy：部署与必要辅助

- `deploy-kkbk-engine.py` 是首次接入时的逐文件部署工具，依赖旧基线目录；已有部署发生变化后不能当作通用更新器直接执行。它尚未覆盖新工作流目录的发布。
- `deploy-harness.py workflows` / `development` 按明确文件集合更新工作流和协调入口，保留逐文件备份及哈希记录；不会自行重启服务，也不是 bot 的自动发布器。
- `deploy-task-assist.py`、`deploy-web-search.py` 部署对应插件。
- `repair-visual-target.py`、`remove-tone-filter.py`、`enable-planner-thinking.py` 仍被任务插件部署流程动态调用，因此保留在同一目录。
- 脚本内部仍有当前 WSL 的路径约定；路径整理不代表已经重新部署或验证跨机器安装。

## 已删除内容

- 退役 Python 沙箱的实现、特权安装器、部署器、配额工具和专用测试；仅保留下架检查。
- 五个没有调用方、仅用于已完成旧版本修补的独立脚本。
- 五个依赖特定历史失败消息或下载研究快照的个案探针。

历史恢复使用 Git。运行数据、登录目录、记忆模块、现有插件与用户未提交修改不随仓库整理删除。运行实例中已复制的旧维护脚本没有在本轮批量清理。

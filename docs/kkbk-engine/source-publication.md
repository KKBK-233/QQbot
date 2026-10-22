# 多仓开发副本与源码发布

本轮实现 Python 宿主与插件的源码发布基础闭环；源码、部署及实群验收分别见 [状态表](status.md)。跨平台接入仍只是计划，本轮没有实现飞书或微信。

## 副本与当前生效基线

主仓 MaiBot 保持在 `ws/` 根，根仓维护代码在 `.project/integration/`，独立插件在 `plugins/`。任务提交时固定每个 Git 仓提交；只导出普通已提交 blob，不复制原仓 `.git`、未提交目录或登录资料。适配器已提交的维护补丁由可信项目清单应用。

维护端清单 `dev_worker/project.wsl.json` 在部署后位于 `/home/qqbot/dev-harness/project.json`，通过 `KKBK_DEV_WORKER_PROJECT_MANIFEST` 可显式替换或设为 `off`。清单的源码、映射、独立测试及服务命令不从群消息或代理产物中读取；现存未配置项目清单的任务继续沿用旧的单仓流程。

组合后，在明确允许的范围同步线上 Python 代码、提示模板、插件 manifest 和当前工作流，统一提交为开发基线。这样可以保留线上尚未进入上游提交的修复，不把这些差异算进候选。线上文件原始摘要、覆盖清单和固定提交记录在代理工作树之外的 `project.json`。真实配置只白名单导出 `personality` 到 `.project-context.json`，不复制模型密钥、实际插件配置或数据库。

`.project-context.json` 是参考快照，修改它不会改动线上人设。提示模板和人设相关源码可以通过源码发布；直接编辑实际 `bot_config.toml` 的字段级配置发布入口尚未实现，应明确说明，不能拿修改参考快照当作已切换人设。

## 从测试到发布

1. 执行配置的主测试及受影响组件检查；图片反查测试必须能导入真实 SDK 和 PicImageSearch，不能以依赖缺失后的整组跳过代替验证。
2. 测试前后重新计算累计补丁摘要；变化则拒绝标为验证通过。独立插件和新插件目录中的新增文件也纳入补丁。
3. 将待发布字节封装到代理不可写的 `tasks/<id>/source-candidate/`，固定提交、补丁摘要、独立检查、上线前摘要和文件映射。后续继续修改开发工作树不会改变旧候选。
4. 按策略串行发布。插件和生图插件目录允许宿主自动发布；引擎、适配器与开发服务本身需要维护端显式 `--allow-core`。根仓的 Windows 图像桥和其他维护脚本尚未配置运行目标，不会复制到一个无效目录后宣称已生效；没有映射的可执行变化明确报告发布阻塞，测试/文档变化单独披露。
5. 发布前比较线上摘要，备份原文件和权限，停止受影响服务，安装或删除文件，再启动服务并检查健康。生产清单的健康检查包括 WebUI、初始化完成和插件加载失败计数；不重启 QQ/SnowLuma。
6. 健康失败自动恢复同批旧文件和依赖指针；恢复遇到其他写入时拒绝覆盖，记录明确的恢复失败。worker 重启会恢复中断的发布并对齐账本；恢复失败不会无限重试，后续发布会要求维护端先处理。

源码发布记录位于 `/home/qqbot/dev-harness/source-releases/`，工作流记录仍在 `workflow-releases/`。二者共用发布锁。开发任务的终态不改写；源码发布结果另存在账本的 `source_publications`，状态查询和群通知分别报告测试与发布情况。

`installed` 表示安装及配置的健康检查通过，业务验收仍为 `pending`。新增工具还须接入宿主发现和引擎工具白名单；涉及白名单的候选属于核心发布范围。不能因为插件文件已落盘就宣布模型已能使用新工具。

## 依赖与恢复

Python 主项目或插件依赖清单变化时，另封装完整已跟踪源码；发布端在版本目录建立独立 venv，并在 bwrap 中安装主项目和插件的 requirements，在断网环境运行配置的依赖检查。最后切换生产 site-packages 中的固定 `.pth` 指针；原环境和旧指针保留，回退同批恢复。

该实现支持 Python 导入依赖的版本切换；不会自动切换所有系统命令、Node/Rust 环境或数据库结构。缺少对应发布步骤的其他生态依赖变化明确拒绝；数据库迁移需要另行设计。插件 pyproject 依赖变化须同步 requirements。生产配置的依赖检查是 `pip check` 和源码编译；涉及兼容性的任务应增加对应集成用例，不能将这两项当作完整业务测试。

开发基座已单独安装与当前宿主一致的 `maibot-plugin-sdk==2.8.2`、`PicImageSearch==3.12.11`，未修改生产依赖。代理仍可在任务私有 venv 中安装任务依赖；独立复测不挂载 DSH 凭据并保持断网。

维护端使用固定的 worker 配置：

```text
python3 -B -m dev_worker publish-source <task_id>
python3 -B -m dev_worker publish-source <task_id> --allow-core
python3 -B -m dev_worker rollback-source <release_id>
python3 -B -m dev_worker recover-source <release_id>
```

命令需在能导入已部署 `dev_worker` 的环境运行，例如设置 `PYTHONPATH=/home/qqbot/dev-harness/worker-code`。发布和恢复命令取得 worker 锁，避免与正在开发的轮次或其他维护发布同时执行。已有后续轮次时拒绝发布旧候选；回退不会覆盖发布后人为修改的文件。源码回退目前使用维护端 CLI，群内 `development_rollback` 仍只处理工作流。

## 验证边界

- 自动化回归使用真实临时 Git 仓、独立进程、候选封装、临时发布目标和依赖 venv；覆盖失败测试、候选篡改、线上冲突、健康失败、回退冲突与中断恢复，不调用模型。
- `scripts/checks/verify-source-project.py` 使用实际多仓与 bwrap、独立账本及任务目录，关闭自动发布，以确定性代码改动验证候选和复测；不调用模型、不向群发消息、不改生产代码。
- `scripts/checks/verify-source-release-live.py` 默认不写入；显式 `--apply-and-rollback` 后在没有排队/运行任务时，用只加注释的候选真实发布并立即回退。会停启 MaiBot、保留发布与回退证据，不调用模型或发送群通知，QQ/SnowLuma 保持原进程。
- 自动化和副本验证不等于 DSH 模型开发成功或实群原场景验收。完整记录应继续覆盖管理员需求、代理开发、候选、发布回执、工具发现和实际使用结果。

# 新引擎运行接入

正式群：1022382380、894179464、427204073。通过 HeartflowManager 直接创建
KKBKHeartFlowChatting，其余会话仍沿用原运行时。没有启用影子模式。

## 本次范围

- 轻路径沿用 planner 的 DeepSeek 配置，一次请求输出 silent/reply/escalate。
- 重路径复用四插件和原有记忆处理器，在原生 Context Items 中保存模型思考、工具调用和结果，完成后直接回答。
- 任务识图与生图增加 kkbk.before_request；任务识图增加 kkbk.after_turn，当前任务结束后清理视觉目标。
- 保留80条历史上限、防抖3秒。仅当前模型请求最多补试一次；已完成的工具不随模型重试重放。
- 发送沿用 send_service；异步图片交付继续回写历史。管理清理上下文、频率调整保留兼容入口。
- 模型、人设、QQ登录、现有沙箱配额和资源限制没有修改。
- 本次完成运行接入，没有另行实现夜间摘要流水线，也没有精简长期记忆后端。

## 源码与部署

- 引擎源码在 MaiBot/src/kkbk_engine，核心仓分支 codex/kkbk-engine-integration。
- 生图钩子源码在 image-tool/maibot-plugin/plugin.py。
- 任务识图插件已有其他未提交改动，因此只将本次增量保存为 task-assist.patch；没有把该插件之前的修改混入提交。
- deploy-kkbk-engine.py 先对照运行快照检查四个已有文件，再逐文件暂存、部署并核对字节。
- 部署清单和原文件保存在 data/kkbk-cutover；不复制配置、凭据、聊天或媒体。
- 只重启 maibot。QQ pid 87、SnowLuma pid 222 在本次部署前后保持不变。

## 验证记录

- 门控、动作、转发和真实14天回放：41项通过。
- verify-kkbk-engine.py 在生产虚拟环境中模拟发送，覆盖单次轻回复、失败补试、工具成功后回答失败不重复执行、真实身份绑定和记忆工具目录。
- --live 使用现有 DeepSeek 验证 JSON、原生工具调用与结果回填；测试工具为本地模拟，不发送QQ消息。
- 新进程启动完成，四个扩展插件加载成功、失败0。完整群聊效果由用户继续测试。

日志通过 kkbk_engine 分类记录 model、tool、decision 和真实发送回执；既有用量表继续记录 token 和缓存。

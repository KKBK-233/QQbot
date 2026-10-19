# OpenCode Go 的 Haiku 5.5 可选接入

此目录提供 MaiBot 的原生 Anthropic Messages 客户端，模型固定为 `claude-haiku-5-5`，套餐地址固定为 `https://opencode.ai/zen/go/v1/messages`。不会静默换型号、切换到 Zen 按量付费或自动重试。

## 当前状态

- 已安装独立 provider `OpenCodeGo` 和可选模型 `opencode-go-haiku-5.5`。
- 2026-10-17规划器单独切为 `DeepSeek / deepseek-flash-thinking`（请求 `deepseek-flash`、思考low档，官方别名对应V4.1-Flash-1017），回复同样切为新版 `DeepSeek / deepseek-flash`，保持非思考模式；其余7类已启用任务继续使用 `opencode-go-haiku-5.5-medium`，实际返回 `claude-haiku-5-5`。OpenCode继续使用代理，DeepSeek直连。语音及两种嵌入任务未启用，生图保持Sunburst / medium。
- 10月17日分两次分别只修改 `planner.model_list` 和 `replyer.model_list`，隔离工具选择、ASCII任务编号、工具结果续接以及实际规划和回复路由已验证。回复切换的备份位于WSL `data/model-route-backups/deepseek-replyer-1017-*/model_config.before.toml`，报告为本地忽略目录 `data/deepseek-replyer-1017/verification.json`。回退备份位于WSL `data/model-route-backups/deepseek-planner-1017-*/model_config.before.toml`，验收报告为本地忽略目录 `data/deepseek-planner-1017/verification.json`。10月16日的Haiku/DSF混合路由9项验收保留在 `data/image-recovery-20261016/mixed-verification.json`。以下全量切换命令与记录属于历史方案，会同时覆盖规划和回复任务，不用于当前混合路由。
- 全量恢复可运行 `activate.py --rollback-deepseek --all-tasks`，只恢复已启用的已知辅助任务，不覆盖供应商、凭据或未启用任务。`verify_all_tasks.py --deepseek` 检查九类实际返回模型、JSON 和图片识别；不是群聊长期效果评测。
- `activate.py --medium --all-tasks` 可复现全量切换；不带 `--all-tasks` 时只切换规划和回复。全量切换保留各任务参数并保存私有完整配置备份；`--rollback-deepseek` 默认只回退主会话，附加 `--all-tasks` 才恢复七类辅助任务。
- 历史全量切换曾将规划器输出上限从16384调为8192 tokens；本次混合路由切换保留现有任务参数、人设、共享记忆和生图配置。仅重启MaiBot，QQ/SnowLuma进程保持在线。
- 使用真实客户端标识 `QQbot-OpenCode-Integration/1.0`；以宿主会话编号的哈希生成稳定 `x-opencode-session`，不冒充 OpenCode、Claude Code 等客户端。
- 后台记忆、人物画像等调用缺少聊天会话编号时，按任务和业务类型生成稳定编号并哈希；有聊天会话编号时优先保留，规划及回复不能使用后台编号兜底。
- OpenCode Go 官方说明要求典型编码代理流量。当前只完成合成编码任务验证，不能据此认定它适合长期提供普通 QQ 群聊。用途依据见 [官方 Go 文档](https://opencode.ai/docs/go/#where-can-i-use-it)。
- 密钥在 WSL `~/.config/maibot-opencode-go/api-key`，文件权限 0600，目录权限 0700。不写入仓库或模型配置；测试、诊断和 HTTP 错误不输出令牌或上游错误正文。
- OpenCode 显式使用 WSL 本机代理 `http://127.0.0.1:2081`，桥接至 Windows `127.0.0.1:2080`；忽略环境代理覆盖和 `NO_PROXY`。DeepSeek 保留独立 `2082` 本机通道，由 Windows 直接连接官方 API，不走外部代理节点。TLS 正常验证，不跟随重定向。

## 验证结果

`1.22.1` 网络分流验证：22 项离线检查通过（14 项客户端、6 项缓存探针、2 项 DeepSeek 通道）；OpenCode medium 的实际工具调用及原生续接通过代理成功，耗时约 2.18/3.22 秒，没有执行工具、发送 QQ 消息或写记忆。DeepSeek 经原来的 2082 通道匿名查询官方接口返回 HTTP 401，确认连接可达，未验证其密钥或生成能力。仅重启 MaiBot 加载修改，QQ 与 SnowLuma 进程未变，管理鉴权及 QQ 接入检查通过。

全量切换验证：51 项离线回归通过；`verify_all_tasks.py --preflight` 验证后台无聊天会话的 JSON 提取和实际图片识别，切换后 `verify_all_tasks.py` 对 9 类任务逐项调用均返回 `OpenCodeGo / claude-haiku-5-5`。图片中的红色圆形和蓝色方形识别正确，8 类文本任务均返回可解析的预期 JSON。切换后实际 `A_Memorix.EpisodeSegmentation` 请求也已使用 medium；这些预检不代表完整群聊或长期记忆效果评测。

复现全量切换：先运行 `verify_all_tasks.py --preflight`，再运行 `activate.py --medium --all-tasks`，仅重启 MaiBot，最后运行 `verify_all_tasks.py`。配置备份位于 WSL 私有 `data/opencode-go/model-config-before-switch-*.toml`；本地验证报告位于忽略目录 `data/opencode-go/all-tasks-{preflight,verification}.json`，只包含合成校验和计数。

2026-10-07 完成：

| 测试 | 结果 |
| --- | --- |
| Windows 最小代码修复 | HTTP 200，返回 `claude-haiku-5-5`，约 1.85 秒，64 输入 / 120 输出 tokens |
| MaiBot 原生工具调用 | 返回 `probe_tests(module="first.py")`，约 7.15 秒 |
| MaiBot 工具结果续接 | 约 2.08 秒，正确说明模拟测试通过，未声称真正执行代码 |
| 两轮宿主验证用量 | 1076 输入 / 123 输出 tokens；仅这次成功双轮测试，不含前面调试请求 |
| 离线回归 | 10 项协议测试 + 8 项生产 SDK / 模拟 HTTP 测试通过 |

未执行模型工具、未发送 QQ 消息、未写聊天记忆。具体用量和套餐权限由服务端决定。公开模型列表当时未列出 Haiku，但真实 Messages 调用成功；不要仅靠模型列表判断授权。

初始别名 `opencode-go-haiku-5.5` 保留关闭思考，便于对照。Low 别名已适配原生 `thinking` 和 `redacted_thinking`，签名及完整 assistant 内容通过宿主已有 `ProviderOpaqueItem` 保存与回放，不混入群聊正文。回放限定相同供应商、端点和型号；文本、工具参数被修改时明确报错，不回放失配内容。

Low 切换验证：38 项离线回归通过，覆盖遮蔽思考、签名、并行工具结果、插件往返、参数白名单及可见正文隔离。真实模型工具调用及续接通过，实际 JSON 含 adaptive/low，续接 assistant 数组与首轮原生响应完全一致。加载后规划器、回复器实际路由验证各约 1.63/1.81 秒；只有 MaiBot 重启，QQ/SnowLuma 进程未变。首次预检遇到一次网络超时，复测通过；这些探针不等于复杂群聊效果评测。

## 部署与复测

以下命令在 Ubuntu 的 `qqbot` 用户下执行：

```sh
# 只安装 provider 与可选模型，绝不切换任务。
/home/qqbot/QQbot/MaiBot/.venv/bin/python /mnt/d/code/web/QQbot/opencode-go/deploy.py
# 离线验证，不读取真实密钥、不联网。
/home/qqbot/QQbot/MaiBot/.venv/bin/python /mnt/d/code/web/QQbot/opencode-go/test_client.py -v
/home/qqbot/QQbot/MaiBot/.venv/bin/python -m unittest discover -s /mnt/d/code/web/QQbot/opencode-go -p test_protocol.py -v
# 两次真实套餐模型请求，只发送合成编码任务，消费套餐额度。
/home/qqbot/QQbot/MaiBot/.venv/bin/python /mnt/d/code/web/QQbot/opencode-go/verify.py
```

验证报告保存在 WSL `MaiBot/data/opencode-go/verification.json`，只包含合成测试结果及计数。部署前的模型配置备份也在这个受保护目录中，含其他 API 凭据，不能上传或提交。

## 边界与回退

当前实现支持文本、内嵌图片、函数工具定义、工具调用及结果续接，支持白名单内的 thinking 与 low/medium/high effort；图片只有离线结构测试，未做真实视觉评测。不支持流式、自定义解析器、任意额外参数、结构化输出、嵌入和音频。输入不满足约束时明确报错，不静默删内容。输出预算上限 8192 tokens，思考计入该预算，响应截断或型号不一致视为失败。

主会话试用切换和回退命令如下；回退时只恢复 DeepSeek 规划器、回复器及规划器输出预算，不覆盖其他配置：

`1.21.1` 移除了任务插件中旧的语气历史过滤：它会删除助手正文却留下原生思考回放，导致连续请求校验失败。`remove-tone-filter.py` 同时清理本地部署源与 WSL 运行目录，私有备份在各自的 `data/tone-filter-backups/`。任务优先和引用识图保留，人设不变。原生正文及工具投影完整时继续原样回放；无工具的旧残缺历史退回宿主仍保留的文字，不重新恢复已删除的旧分析；工具或签名损坏仍明确报错。

```sh
# 切换前预检工具能力和短文本；仅合成输入，不执行工具或发送群消息。
/home/qqbot/QQbot/MaiBot/.venv/bin/python /mnt/d/code/web/QQbot/opencode-go/verify_route.py --preflight
# 私有备份并切换主会话模型。
/home/qqbot/QQbot/MaiBot/.venv/bin/python /mnt/d/code/web/QQbot/opencode-go/activate.py --low
supervisorctl -c /home/qqbot/QQbot/supervisord.conf restart maibot
# 验证实际任务配置路由，不使用模型覆盖。
/home/qqbot/QQbot/MaiBot/.venv/bin/python /mnt/d/code/web/QQbot/opencode-go/verify_route.py
# 真正验证Low原生工具续接，不执行工具或发送群消息。
/home/qqbot/QQbot/MaiBot/.venv/bin/python /mnt/d/code/web/QQbot/opencode-go/verify.py --model opencode-go-haiku-5.5-low
# 需要恢复 DeepSeek 时执行，再仅重启 MaiBot。
/home/qqbot/QQbot/MaiBot/.venv/bin/python /mnt/d/code/web/QQbot/opencode-go/activate.py --rollback-deepseek
```

本次预检与切换后验证均通过：实际返回 `claude-haiku-5-5`，规划器正确返回原生工具调用，回复器按指定短格式返回。切换后两次合成调用耗时约 2.00/1.85 秒；没有执行工具、发送 QQ 消息或写聊天记忆。结果在本地忽略目录 `data/opencode-go/{preflight,route-verification}.json`，生产私有切换记录在 WSL `MaiBot/data/opencode-go/activation.json`；缓存观察从用量 ID 6736 后开始，统计时排除合成探针。

需要完全移除时，先回退任务，再从 WSL 模型配置删除 `OpenCodeGo` provider 与 `opencode-go-haiku-5.5` 可选模型并删除独立凭据；不要覆盖整个模型配置或撤销其他账号授权。

## 缓存实测

`1.21.2` 修复裁切历史从 assistant 开始时回复器本地校验失败：在此前对话前加一条中性历史起点，保留原助手角色、正文、思考签名和工具结果；缺失工具结果仍拒绝发送。44 项回归通过，真实失败回复快照（39 项上下文、9774 输入 tokens）重放成功，没有执行工具、写记忆或发送 QQ 消息。可用 `verify_history_window.py` 重放最近同类失败快照，只输出计数。

OpenCode 连接遥测使用 `opencode_go_transport` 日志，仅含 TCP/TLS/请求/响应阶段、连接目标地址、耗时和 HTTP 状态，不记录凭据、提示词或响应正文。此前直连匿名探测发现部分地址 TCP 超时，但无法确定每次故障连接了哪个地址。`1.22.1` 按用户指定改为固定代理；不固定上游 IP、不关闭 TLS 校验、不增加模型请求重试。

固定前缀缓存已在 `1.20.1` 部署，当前生产客户端会给工具末端和固定系统前缀添加显式标记，动态时间、@提醒及生图状态保持在其后。Low 保留这一布局；更改思考/effort 后需要重新预热，不能沿用关闭思考的命中率。后续生产检查及关闭思考/adaptive 的隔离对照见 [排查记录](diagnosis.md)；下述无标记结果是切换前基准。

缓存结果见 [实测记录](cache-audit.md)。切换前无缓存标记的三轮合成编码请求命中率0%；显式标记固定system前缀后，第一轮写入9600 tokens，后两轮各读取9600 tokens，预热后输入token命中率99.69%。含冷启动的三轮加权token命中率66.46%，请求命中率66.67%。本次切换没有增加显式缓存标记，不能沿用 DeepSeek 的生产命中率或把隔离实验当作 Haiku 群聊命中率。此前6项缓存探针离线测试通过。
# 原生回合签名恢复

若上游在工具结果回传后明确返回 HTTP 400，且错误同时包含 `invalid`、`thinking`、`signature`，客户端保留已核对的可见文字、工具调用编号、参数及工具结果，只对当前请求改用可移植历史并重试一次。成功后在内存标记这些旧回合，避免继续回放被拒绝的签名；新回合仍保留原生内容。模型、思考强度和温度不变，不修改聊天历史数据库。

其他 HTTP 错误或内容拒绝不进入此恢复分支；恢复失败不循环重试。日志只记录状态码和已知协议词，不打印上游错误原文、用户正文、凭据或签名。异常回合编号缓存最多 512 个，仅在当前进程中生效。


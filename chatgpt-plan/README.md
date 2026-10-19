# MaiBot 的官方 ChatGPT plan provider

2026-10-15按用户要求，当前仅 `replyer` 改用 `DeepSeek / deepseek-v4-flash` 非思考模式。`planner`、`memory`、`mid_memory`、`utils`、`learner`、`expression_use`、`emoji`、`vlm` 这8项继续使用 `gpt-6-luna high`；空置任务保持空置。只修改 `model_task_config.replyer.model_list`，模型定义、provider、超时、温度、图片生成配置及其他字段均与快照逐项核对不变。切换前DSF覆盖模型预检、切换后不覆盖的实际replyer服务调用均通过，分别约1.1秒返回；验证不发QQ、不执行工具、不写聊天记忆。私有原配置保存在WSL `MaiBot/data/model-route-backups/replyer-dsf-1792085720776348359/`，脱敏变更与验证结果在 `data/replyer-dsf-switch/`。只重启MaiBot，QQ和SnowLuma进程保持，管理鉴权及QQ在线检查通过。该切换改变群聊回复模型，工具选择和任务规划仍由Luna处理。

此前全部9项Haiku任务切换为 `gpt-6-luna high`：`planner`、`replyer`、`memory`、`mid_memory`、`utils`、`learner`、`expression_use`、`emoji`、`vlm`。沿用之前验证Sol的个人订阅OAuth接口。未使用的旧模型定义仅保留作历史配置。

真实验证通过：账号模型目录支持 Luna high 与图片输入，两轮模型调用返回 `gpt-6-luna`，模拟工具调用、结果续接和合成红色正方形识别成功；实际 `planner` 服务路由也返回 `ChatGPTPlan / gpt-6-luna`。17 项 provider 检查及 2 项批量替换/回退测试通过。验证请求不执行 QQ 工具、不写长期记忆。切换只重启麦麦。

复现全部 Haiku 替换和回退（在 WSL 中以 qqbot 用户执行）：

```sh
/home/qqbot/QQbot/MaiBot/.venv/bin/python /mnt/d/code/web/QQbot/chatgpt-plan/verify.py --model gpt-6-luna --effort high --with-vision
/home/qqbot/QQbot/MaiBot/.venv/bin/python /mnt/d/code/web/QQbot/chatgpt-plan/deploy.py --activate --model gpt-6-luna --effort high --replace-haiku
/home/qqbot/QQbot/MaiBot/.venv/bin/python /mnt/d/code/web/QQbot/chatgpt-plan/verify_service.py --model gpt-6-luna
supervisorctl -c /home/qqbot/QQbot/supervisord.conf restart maibot
# 回退仅恢复本轮替换的任务模型列表，保留之后对其他字段的调整。
/home/qqbot/QQbot/MaiBot/.venv/bin/python /mnt/d/code/web/QQbot/chatgpt-plan/deploy.py --restore --replace-haiku
supervisorctl -c /home/qqbot/QQbot/supervisord.conf restart maibot
```

`--replace-haiku` 根据模型定义和实际任务引用匹配 Haiku，替换时保留其他候选的顺序、输出上限与任务超时。验证报告绑定型号、思考级别、图片能力和当前订阅账号；备份及回退清单只写入 WSL 受保护的 `MaiBot/data/chatgpt-plan/`。

以下记录为此前 Sol 接入的历史验证。

2026-10-09按用户要求，`planner`、`replyer`、`vlm`切到`gpt-6.1-sol high`，其余记忆/学习任务仍用原模型。当前图片绑定修复、人设和聊天历史保留，不把切换后的单次表现当成训练机制结论。17项离线provider检查通过；真实high工具调用、原生续接和合成红色正方形识别通过，实际任务服务planner返回`ChatGPTPlan / gpt-6.1-sol`。验证不发送QQ、不写记忆、不生图。首次合成验证未通过人物输出校验，调整为同一请求内的图片和工具续接后通过；总共5次短模型调用，未回放整段群聊。只重启MaiBot，QQ保持登录。

复现本轮三项任务切换：先运行`verify.py --effort high --with-vision`，再运行`deploy.py --activate --effort high --with-vision`，只重启MaiBot。激活核对最近一小时的账号、型号、思考级别及视觉验证结果，保存私有时间戳配置快照。回退本轮三项任务使用`deploy.py --restore --with-vision`，恢复最新切换快照中的三项模型列表，再重启MaiBot；不带`--with-vision`仍沿用原来的两项任务回退行为。

新增 `openai_chatgpt_plan` 客户端，直接访问 `https://api.openai.com/v1/responses`。人格、记忆、频率控制、工具调度继续由 MaiCore 管理。此目录保存部署源码，运行文件投放到 WSL MaiBot 的 `src/llm_models/model_client/`。

## 登录

账号页采用 `Referrer-Policy: same-origin`：保留本机表单的来源，跨站授权不发送页面来源。原先 `no-referrer` 会让 Chromium 的 POST 表单携带 `Origin: null`，即使校验值正确仍被拒绝。`test_login_browser.py` 用隔离 Chromium 和模拟账号验证真实表单提交及 303 返回首页。重新登录完成会自动启用账号，当前账号不再显示重复的“使用此账号”按钮。

身份令牌校验允许 30 秒时钟偏差，避免认证服务器比本机快约 2 秒时出现 `ImmatureSignatureError`。签名、issuer、audience、必需声明和登录 nonce 校验保持启用；超出容差的未来令牌与过期令牌仍拒绝。`test_identity.py` 使用本地 RSA 签名验证这项兼容处理及拒绝分支。

请使用已登录 ChatGPT 的日常浏览器（例如 Edge）打开本机账号页。登录服务重启后，旧标签页的表单校验值会失效；旧操作保持拒绝，并提供“重新打开账号页”入口。重新授权链接支持在 Edge 中直接打开，无需先在 Codex 内置浏览器登录。

登录操作地址 `/select`、`/logout`、`/login` 被直接打开或刷新时，以 303 跳转回账号页；账号选择与退出仍仅通过带请求校验的 POST 执行。授权失效后使用原账号下的“重新登录此账号”，“使用此账号”只切换已保存账号，不刷新授权。页面不再固定显示旧型号，模型沿用麦麦当前配置。可运行 `test_login_server.py` 验证跳转、账号选择、请求校验及登录页提示。

在自己的浏览器打开 <http://127.0.0.1:18161/authorize>，使用 **Continue with ChatGPT** 授权。管理账号打开 <http://127.0.0.1:18161/>。登录页仅监听本机，Windows 经 WSL 的 localhost 转发访问。

- 每个 ChatGPT 账号独立动态注册；返回的 issued client ID 与已验证身份绑定。
- 使用 PKCE、state、nonce 和 JWKS 签名校验；复用同一 host ID。
- 凭据位于 WSL `~/.config/maibot-chatgpt-plan/accounts.json`，权限 0600、原子写入；不读取或修改 Codex 的 auth.json。
- 刷新由文件锁串行化，避免主循环、WebUI 与登录服务争抢轮换令牌。
- 登录页提供保存账号选择、重新登录和退出；退出先尝试撤销 refresh token，然后清除本地令牌，保留注册映射。

## 部署与试用

以下命令在 Ubuntu 的 qqbot 用户下执行，Python 使用 MaiBot 的 `.venv/bin/python`。

```sh
/home/qqbot/QQbot/MaiBot/.venv/bin/python /mnt/d/code/web/QQbot/chatgpt-plan/deploy.py
supervisorctl -c /home/qqbot/QQbot/supervisord.conf reread
supervisorctl -c /home/qqbot/QQbot/supervisord.conf update chatgpt-plan-login
/home/qqbot/QQbot/MaiBot/.venv/bin/python /mnt/d/code/web/QQbot/chatgpt-plan/test_provider.py
# 登录后：验证真实模型及模拟工具续接，不发送 QQ 消息，不写记忆。
/home/qqbot/QQbot/MaiBot/.venv/bin/python /mnt/d/code/web/QQbot/chatgpt-plan/verify.py
/home/qqbot/QQbot/MaiBot/.venv/bin/python /mnt/d/code/web/QQbot/chatgpt-plan/deploy.py --activate
supervisorctl -c /home/qqbot/QQbot/supervisord.conf restart maibot
# 可选：穿过实际任务配置验证 provider 路由，返回的模拟工具不执行。
/home/qqbot/QQbot/MaiBot/.venv/bin/python /mnt/d/code/web/QQbot/chatgpt-plan/verify_service.py
```

验证通过才允许激活。切换 `planner`、`replyer` 到 `gpt-6.1-sol`，reasoning 为 `low`；辅助模型配置保留。验证与激活绑定同一个账号。配置备份含已有 API 凭据，仅写在 WSL 受保护的 `MaiBot/data/chatgpt-plan/`，不提交。

恢复原对话模型使用 `deploy.py --restore`，再重启 maibot。只恢复两个任务的模型列表，保留之后用户对其他配置的修改。

## 协议

- 固定 `stream: true`、`store: false`，完整发送 input；省略 `previous_response_id`。
- system 转 developer；固定宿主规则提前放置，文字、图片、工具调用与结果保持顺序、call_id 和原工具名。
- function tools 放入 `maibot` namespace，明确 `strict: false` 保留 MaiBot 可选参数含义。
- 保留加密 reasoning 的原生回放；从 `output_item.done` 汇总输出，只有 `response.completed` 才算成功。
- 省略预览不支持的 temperature、max_output_tokens 等参数；其他未知参数明确报错。
- 该登录路径不支持 embeddings、音频转录或托管 image_generation。现有生图桥接独立运行。
- 登录成功不代表某个模型可调用，必须以一次真实推理及工具续接验证为准。
- 此适配只处理 provider 协议，不改变规划器向回复器传递记忆结果的上层逻辑。

官方依据：[注册与登录](https://developers.openai.com/siwc/token-sharing-open-source/sign-in)、[模型与推理](https://developers.openai.com/siwc/token-sharing-open-source/models-and-inference)、[预览限制](https://developers.openai.com/siwc/token-sharing-open-source/preview-limitations)。

## 提示缓存

2026-10-06 排查：[实测记录](cache-audit.md)。生产缓存计数原本读取正确；优化不删历史、不改记忆索引、人设或聊天频率。

- 将已知插件的固定身份与语气规则放到历史前面；兼容 MaiBot 实际的字符串 system 块和 Responses 的 input_text 块。生图动态状态仍留在原位置，权限保持 developer。其他宿主提醒不移动。
- 按工具名及参数键稳定排列定义，避免插件重载引起无意义变化。
- 按任务设置稳定 `prompt_cache_key`（`planner`、`replyer` 分开），用户显式配置优先。它用于缓存路由，不保证每次命中。
- `data/chatgpt-plan/cache-usage.jsonl` 仅保存服务端计数、任务名和会话哈希，权限 0600。缺少计数记为 null；遥测写入失败只记警告，不重试模型。

当前登录预览通道实测拒绝 `prompt_cache_options` 和 `prompt_cache_breakpoint`（HTTP 400），适配器不发送这些参数。新模型的显式缓存机制与本账号通道的支持程度需要区分，参考[官方缓存说明](https://developers.openai.com/api/docs/guides/prompt-caching)。

```sh
# 查看数据库最新请求之前两小时的加权命中率，不把隔离测试计入群聊统计。
/home/qqbot/QQbot/MaiBot/.venv/bin/python /mnt/d/code/web/QQbot/chatgpt-plan/inspect_cache.py
# 仅看上线之后的新记录；ID 从部署时的数据库 MAX(id) 获取。
/home/qqbot/QQbot/MaiBot/.venv/bin/python /mnt/d/code/web/QQbot/chatgpt-plan/inspect_cache.py --after-id <部署前ID>
# 可选真实回放会消耗模型额度，只生成短文字，不发QQ、不执行工具、不写记忆。
/home/qqbot/QQbot/MaiBot/.venv/bin/python /mnt/d/code/web/QQbot/chatgpt-plan/verify_cache.py --task planner --repeat
```


### 2026-10-07 可回退升级

`1.16.0` 修复了真实字符串 system 块未被前移的问题，`1.16.1` 统一了无 trace 回放的默认缓存键。部署前 provider 副本保存在 WSL：

```sh
/home/qqbot/QQbot/MaiBot/data/chatgpt-plan/rollback/cache-v2-before/
```

如需回退，只恢复其中的 `openai_chatgpt_plan.py` 和 `chatgpt_plan_cache_layout.py`，然后仅重启 MaiBot：

```sh
cp /home/qqbot/QQbot/MaiBot/data/chatgpt-plan/rollback/cache-v2-before/openai_chatgpt_plan.py /home/qqbot/QQbot/MaiBot/src/llm_models/model_client/openai_chatgpt_plan.py
cp /home/qqbot/QQbot/MaiBot/data/chatgpt-plan/rollback/cache-v2-before/chatgpt_plan_cache_layout.py /home/qqbot/QQbot/MaiBot/src/llm_models/model_client/chatgpt_plan_cache_layout.py
supervisorctl -c /home/qqbot/QQbot/supervisord.conf restart maibot
```

升级后的隔离 planner 回放为相同 14003 输入 token、两次各命中 2944；replyer 相同 8319 输入 token 的两次回放仍为 0。两者均未发送 QQ、执行工具或写入记忆，真实自然聊天仍需继续观察。

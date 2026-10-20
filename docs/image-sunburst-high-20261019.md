# Sunburst / high 试验与恢复medium记录

当前生产配置为Sunburst / medium：10月19日完成下述high试验后，管理员要求继续使用中档，已只恢复 `codex_image_quality=medium` 并仅重启MaiBot加载。以下保留本次试验的请求与实际返回，避免将历史high请求当成当前配置。

2026-10-19按管理员要求提高生图精度。整理后的项目入口仍为
`image-tool/maibot-plugin/plugin.py` → `image-tool/bridge.py` → 原生Codex Images接口。
生图继续使用现有Codex订阅OAuth，生成请求走 `images/generations`，参考编辑走 `images/edits`。

## 配置

- 型号保持 `gpt-image-2.5-sunburst`。
- 画质请求从 `medium` 提高到 `high`；这是本次唯一生产配置变更。
- 接口、代理、并发、角色卡和文字模型路由保持原配置。
- 配置修改前留存备份，确认无排队或运行中的生产生图任务后仅重启MaiBot，保留QQ/SnowLuma登录。

## 实测证据

隔离复用之前的DS娘海港书店提示词，只生成一张，不发送QQ。
本轮请求一次，HTTP 200，耗时49.7秒，得到可解码的1536×1024 PNG；人工查看确认正常成图。
服务端报告 `quality=medium`、`background=opaque`，没有报告具体型号。
因此只确认Sunburst/high请求已经提交且能够成图，不能声称服务端已兑现high，或账号已进入特定灰测。

本地忽略目录 `data/sunburst-high-20261019/` 保存提示词、配置备份、激活记录、
`verification.json`、单次请求诊断和成图。成图位于
`jobs/sunburst-high-controlled.png`。已有其他未提交的生图审计源码修改不属于本次提交。

公开API与当前订阅私有接口的行为需分别核对。
[OpenAI官方生图指南](https://developers.openai.com/api/docs/guides/image-generation)
列出Sunburst和high画质参数，但不能据此推断订阅接口实际选型或账号灰测状态。

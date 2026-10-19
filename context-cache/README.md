# 会话缓存布局与DeepSeek回切

只移动可识别的固定宿主规则、稳定工具注册顺序，把宿主时间和规划器本轮规则作为独立后置上下文。OpenAI ChatGPT plan 将 system 投影成 developer；DeepSeek Chat Completions 使用 system。真实聊天、图片、工具调用及结果、网页正文、记忆引文继续保留原有身份和相对顺序。不更改人设、跨会话记忆范围、历史窗口、压缩或工具执行逻辑。

规划器仍使用 Flash 思考模式，回复器仍使用 Flash 非思考模式。此次只回切 planner/replyer，其他任务不变。指定模型 ID 为 `deepseek-v4-flash`。DeepSeek 官方说明该旧别名现由 V4.1 Flash 服务；实际接口返回的模型名称是 `deepseek-flash`。没有强行指定已退役的权重版本。

`deploy.py` 默认仅验证补丁；`--apply` 备份并应用四个源码文件；`--activate` 在12次同模型对照成功后验证配置、备份并切换会话模型。生产备份与用量基线保存在 `/home/qqbot/QQbot/MaiBot/data/context-cache/`。源码与配置分别备份，回滚应只还原对应文件和planner/replyer模型列表，保留后续其他配置修改。

验证包括工具结果原子顺序、图片引用、外部引文身份、即时状态更新、原生思考投影，以及实际MaiCore工具/回复路由。测试不执行返回的工具、不写记忆、不发送QQ消息。

`benchmark.py` 最多12次真实DeepSeek调用：planner/replyer各旧、新两组，每组冷请求、完全相同重发、更新时间并追加消息。使用真实固定规则与工具，聊天内容为合成数据，仅输出OK。模型、温度、思考设置和网络通道在旧/新组间一致。该结果说明布局对这组前缀的收益，不是自然群聊长期命中率。

DeepSeek缓存由服务器自动管理。官方文档说明完整前缀持久化、跨请求共同前缀检测和固定长度前缀持久化都可能产生命中，写入需要时间，按best-effort提供。因此不使用显式缓存参数，也不根据单次未命中推断缓存被关闭。

参考：[DeepSeek缓存](https://api-docs.deepseek.com/guides/kv_cache/)、[模型别名](https://api-docs.deepseek.com/api/responses)。测试结果保存在本地忽略目录 `data/deepseek-layout-benchmark/`，不把运行配置、聊天记录或凭据提交到Git。

## 2026-10-07部署验证

12次同模型对照全部成功，实际接口返回 `deepseek-flash`，各次发送载荷已核对。更新时间并追加消息的第3次请求：

| 任务 | 旧布局 | 新布局 | 增加百分点 |
|---|---:|---:|---:|
| planner（思考） | 4736 / 5856 = 80.87% | 5632 / 5861 = 96.09% | 15.22 |
| replyer（非思考） | 4096 / 5291 = 77.41% | 4992 / 5296 = 94.26% | 16.85 |

完全相同请求重发时，两种布局均约96%～97%；第一次请求仍只有约5%～13%命中。不能把这组结果解释成长期自然群聊稳定命中率，单个样本也不能拆分规则前置、状态后置、工具排序各自的贡献。

部署后，实际MaiCore planner返回原生模拟工具调用（未执行），replyer遵守指定短格式。所有11项本地结构/协议检查通过。生产配置验证和源码哈希核对通过，切换基线为 `llm_usage.id=5790`，部署审计时尚无后续自然planner/replyer样本。只重启MaiBot，QQ和SnowLuma进程保持运行，审计时最近250行日志没有ERROR/Traceback。

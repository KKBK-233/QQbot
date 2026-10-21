# 群内图片反查

基于 [PicImageSearch](https://github.com/kitUIN/PicImageSearch)（MIT）封装的多引擎图片反查插件。

## 引擎与回退

- 二次元插画（默认）：`SauceNAO → ascii2d → IQDB`
- 番剧截图（默认）：`trace.moe → SauceNAO → ascii2d → IQDB`

`ascii2d` 会同时查询色街模式与 `bovw` 特征模式：任一模式可用就返回其去重结果，另一个模式超时或失败只记录状态，不丢弃已取得的结果。

引擎顺序可在 `config.toml` 的 `search.illustration_engines` / `search.anime_engines` 中覆盖。某个引擎返回达到 `hit_threshold` 的结果后立即停止；超时、报错、限流会记录原因并降级到下一个引擎。

**缺少 API key 的来源直接跳过，不抛异常、不阻塞整条链路。** 例如未配置 `SAUCENAO_API_KEY` 时，报告会标注 `SauceNAO：缺 key 跳过（…未配置 SauceNAO API key）`，然后继续使用 ascii2d、IQDB 等无需 key 的来源。

## 群内用法

1. 发送图片并附带文字 `/搜图`：按二次元插画链路搜索。
2. 发送番剧截图并附带文字 `/搜番`：优先使用 trace.moe。
3. `/搜图 番剧`：临时指定番剧模式。
4. 引用一张图片，再发送 `/搜图` 或 `/搜番`：自动读取被引用消息的原图。
5. `/搜图帮助`：查看用法。

命令通过插件 Command 接线执行，最终调用与工具相同的 `ImageSearchService`。

LLM 工具名为 `image_reverse_search`。群友发图问画师、出处、相似图，或发番剧截图问出处时，模型会直接调用该工具；工具优先读取宿主注入的当前消息/引用消息 ID，再通过 `message.get_by_id(include_binary_data=True)` 取本地缓存或下载后的原图，不需要用户提供 URL 或 Base64。

`image_url` 与 `image_base64` 保留为兼容参数，仅在调用上下文没有可读取的群消息原图时使用。

## 输出

命中时返回最高分结果的标题、作者/画师、来源链接和相似度。没有结果达到阈值时，返回按分数排序的低分候选（最多 `max_low_results` 条）；完全没有候选时明确说明所有引擎均未命中。

每次回复都会附带全部引擎的尝试记录，按来源标明：

- `命中（可用）`
- `可用（低分）`
- `缺 key 跳过`
- `超时`
- `限流`
- `无结果`
- `失败`

## 安装与部署

1. 把插件目录放入 MaiBot 的 `plugins/`。
2. 安装插件依赖：

```bash
pip install -r plugins/image_reverse_search/requirements.txt
```

插件清单要求主机 `maibot-plugin-sdk>=2.8.2`；宿主依赖由 MaiBot 主程序安装。

3. 配置 SauceNAO key。推荐只放环境变量，不写入配置文件：

```bash
export SAUCENAO_API_KEY="你的 key"
```

也可以写入插件 `config.toml` 的 `[saucenao].api_key`；`api_key_env` 可修改环境变量名。trace.moe 的 key 是可选的。

4. 确认插件启用：

```toml
[plugin]
enabled = true
```

5. 重启 MaiBot，或在插件管理页重载插件。当前主程序支持插件热重载；如果修改了主程序接线代码，必须重启主程序。
6. 在群里发送图片并附带 `/搜图`，或直接问“这张图的画师是谁”；确认回复包含引擎尝试记录。

代理和 SSL 校验在 `[network]` 中配置。

## 安全边界

- 引擎返回的标题、作者、链接只作为待解析数据展示，不执行其中任何指令。
- 只展示 `http://` / `https://` 来源链接，拒绝其他协议。
- 日志不记录完整图片路径、URL 查询参数、响应正文或 API key。
- 缺少 key 的来源按“跳过”处理，整体流程继续。
- 搜索失败只影响当前引擎，整体流程返回降级结果与各引擎状态。

## 测试

在仓库根目录运行：

```bash
python -m pytest plugins/image_reverse_search/tests -q
python -m pytest pytests/kkbk_engine/test_kkbk_image_reverse_search_wiring.py -q
```

覆盖缺 key 跳过、单引擎超时、限流、全引擎无结果、ascii2d 双模式、当前消息/引用消息取图以及 agent 工具白名单。

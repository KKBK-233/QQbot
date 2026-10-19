# QQbot：MaiBot 衍生引擎与工作流

QQ / SnowLuma 提供平台接入，MaiBot 提供消息、存储、模型、记忆和插件基础设施。指定群使用 KKBK 轻层与任务层；后续独立 DeepSeek Harness 负责开发和更新整个项目。

公开快照的来源版本见 [SOURCE_SNAPSHOT.json](SOURCE_SNAPSHOT.json)，许可证见 [LICENSE](LICENSE)。

## 当前进度

- 轻层与任务层已接入三个指定群，保留 3 秒防抖、80 条历史和原生工具调用。
- 生图、搜索、按需识图三个业务插件保留；现有长期记忆保持原状。
- 版本化工作流已实现并测试，尚未部署；开发代理、教学自动更新与发布器尚未实现。
- 离线 Python 沙箱已下架，其源码及专用安装/部署/测试代码已删除。历史实现可从 Git 恢复。
- 其他精简候选仅盘点，没有批量关闭。

## 项目入口

- [完整实现计划与验收记录](docs/kkbk-engine/harness-plan.md)
- [当前功能与精简候选](docs/kkbk-engine/feature-inventory.md)
- [引擎接入说明](docs/kkbk-engine/README.md)
- [脚本目录与使用范围](scripts/README.md)
- [本轮目录整理说明](docs/kkbk-engine/repository-layout.md)（历史个案仅保留本地）
- [公开源码仓库](https://github.com/KKBK-233/QQbot)

## 目录

| 目录或入口 | 用途 |
| --- | --- |
| `MaiBot/` | 独立 Git 仓的本地源码，含 `src/kkbk_engine`、工作流与测试 |
| `image-tool/` | 生图桥接及生图插件源码 |
| `chatgpt-plan/`、`opencode-go/` | 当前模型/订阅接入与验证工具 |
| `scripts/deploy/` | 部署入口及仍被部署流程调用的兼容辅助代码 |
| `scripts/checks/` | 管理鉴权、运行注册、请求兼容与诊断入口 |
| `tests/` | 根项目的离线单元测试 |
| `memory-fix/`、`gif-fix/`、`context-cache/` | 仍需保留的记忆、媒体与缓存补丁工具 |
| `docs/` | 计划、功能清单和历史说明 |
| 根目录启动脚本、模型桥接 | 已被 WSL/Supervisor 引用，固定路径保留 |

本地源码、运行实例和公开快照是不同位置。公开仓库包含已提交源码的导出副本；运行配置、聊天记录、登录信息和未提交修改不上传。

## 日常操作

在 Windows 项目根目录执行：

```powershell
.\启动机器人.ps1
.\查看管理登录.ps1
wsl -d Ubuntu -u qqbot -- python3 /mnt/d/code/web/QQbot/scripts/checks/check-wsl.py
```

管理页面：MaiBot `http://localhost:8001`，SnowLuma `http://localhost:5099`。

QQ 使用现有登录目录，启动后先检查恢复状态；只有客户端要求验证时才重新登录。只修改模型或机器人源码时，按需要重启 `maibot`，不重启 QQ/SnowLuma。

## 离线验证

根项目：

```powershell
python -X utf8 -B -m unittest discover -s tests -p "test_*.py"
```

引擎与工作流：

```powershell
cd MaiBot
python -X utf8 -B -m pytest -q --noconftest -p no:cacheprovider pytests/kkbk_engine
```

真实群聊回放需要本地私有数据，通过 `KKBK_REPLAY_EXPORT` 指定；公开快照缺少该数据时跳过对应项。微信人工交付测试依赖本地搜索插件源码，公开快照缺少该源码时跳过对应测试。检查脚本的 `--live`、浏览器交付和部署入口需按其说明单独使用，不由单元测试自动触发。

## 来源

MaiBot 上游：[Mai-with-u/MaiBot](https://github.com/Mai-with-u/MaiBot)，当前衍生引擎基线 `bac9090`。组件许可证及来源信息见各自目录；公开源码快照另附 `SOURCE_SNAPSHOT.json`。

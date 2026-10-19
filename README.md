# QQbot：MaiBot 衍生引擎与工作流

基于 MaiBot 的 QQ AI 项目公开源码快照，用于代码审阅和与网页版 ChatGPT 同步设计。

2026-10-19：离线 Python 已下架，其他额外功能待精简选择。见[功能清单](docs/kkbk-engine/feature-inventory.md)。

## 从这里开始

- [完整实现计划与进度](docs/kkbk-engine/harness-plan.md)
- [当前引擎接入说明](docs/kkbk-engine/README.md)
- [工作流格式、启用与版本更新](MaiBot/docs/kkbk-workflows.md)
- [日常层与任务层运行代码](MaiBot/src/kkbk_engine/runtime.py)
- [工作流加载器](MaiBot/src/kkbk_engine/workflows.py)
- [启用清单](MaiBot/workflows/enabled.json)
- [搜索工作流示例](MaiBot/workflows/catalog/conservative-web-research/1.0.0.json)

## 当前进度

日常和简单任务层复用 MaiBot 的消息、模型、记忆和插件设施。指定群的新引擎已经接入；版本化工作流源码已完成，80 项本地测试通过，其中真实 14 天回放的数据不公开。本次工作流改动尚未部署到线上。

后续依次建设独立开发工具服务、教学任务协调、验证发布与恢复、主动改进。开发层最终允许修改完整项目工作副本，不局限于插件。现有记忆模块保留。

## 快照范围

来源版本及范围见 [SOURCE_SNAPSHOT.json](SOURCE_SNAPSHOT.json)。`MaiBot/` 是该提交的完整源码导出；仓库根目录提供已提交的本地集成代码和相关说明。

本快照不包含运行配置、凭据、聊天导出、数据库、日志或未提交修改。它不是生产目录的逐字镜像；部分历史集成依赖本地未提交补丁或插件，因此不能承诺直接克隆即复现完整线上部署。部署脚本仍含本地路径约定，使用前需配置环境。

## 工作流测试

```powershell
cd MaiBot
python -m pip install pytest
python -X utf8 -B -m pytest -q --noconftest -p no:cacheprovider pytests/kkbk_engine
```

未提供私有回放数据时，对应回放项会跳过；纯工作流与运行时模拟测试不需要 QQ 登录或模型密钥。

## 来源与许可证

MaiBot 上游：[Mai-with-u/MaiBot](https://github.com/Mai-with-u/MaiBot)，基线 `bac9090758668098003d59642b607fb0f1b77337`。保留上游源码、版权和各组件许可证；整体衍生代码按 [GPL-3.0](LICENSE) 提供，具体组件同时见其目录内许可证。

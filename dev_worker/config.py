"""开发 worker 的路径与运行参数。

默认值指向 WSL 内 qqbot 用户的开发基座；所有项都能用环境变量覆盖，
以便在 Windows 上离线跑单元测试。生产目录不作为开发读写目标，
真实执行时还会通过独立挂载视图隐藏生产和 Windows 源码路径。
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path, PurePosixPath

# 开发基座：dsh 安装、DSH_HOME、任务目录、测试用基础 venv 都在它下面。
DEFAULT_ROOT = "/home/qqbot/dev-harness"
# 工作副本的克隆来源：Windows 侧的工作仓库，只读使用。
DEFAULT_SOURCE_REPO = "/mnt/d/code/web/QQbot/MaiBot"
DEFAULT_BASE_REF = "codex/kkbk-engine-integration"
# 默认只跑引擎自己的测试，避免一次任务拖进全量套件。
DEFAULT_TEST_COMMAND = "-m pytest -q --noconftest -p no:cacheprovider pytests/kkbk_engine"
# 只接受管理员提交的开发任务。真实身份由宿主执行上下文确认，阶段 3 接入，
# 这里只做一层白名单，不把聊天里的自称当凭证。
DEFAULT_ALLOWED_REQUESTERS = ("1020050112",)
# Linux 侧固定 PATH：WSL 会把 Windows 的 PATH 追加进来，必须排除，
# 否则会命中 /mnt/c 上的旧版 dsh。
DEFAULT_AGENT_PATH = "/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin"
DEFAULT_PIP_INDEX = "https://mirrors.aliyun.com/pypi/simple"
# 工作流候选通过测试后自动发布到这里；设为 off 关闭自动发布。
DEFAULT_LIVE_WORKFLOWS = "/home/qqbot/QQbot/MaiBot/workflows"
DEFAULT_LIVE_LOADER = "/home/qqbot/QQbot/MaiBot/src/kkbk_engine/workflows.py"

# 环境变量前缀
ENV_PREFIX = "KKBK_DEV_WORKER_"


def _env_int(environ: dict[str, str], name: str, default: int) -> int:
    raw = environ.get(ENV_PREFIX + name)
    if not raw:
        return default
    try:
        value = int(raw)
    except ValueError as exc:  # 配置写错时直接报出来，不静默回退
        raise ValueError(f"{ENV_PREFIX + name} 不是整数：{raw!r}") from exc
    if value <= 0:
        raise ValueError(f"{ENV_PREFIX + name} 必须为正整数：{value}")
    return value


def _env_path(environ: dict[str, str], name: str, default: str) -> Path | None:
    raw = environ.get(ENV_PREFIX + name, default)
    return None if raw.strip().lower() == "off" else Path(raw)


def _env_bool(environ: dict[str, str], name: str, default: bool) -> bool:
    raw = environ.get(ENV_PREFIX + name)
    if raw is None or raw == "":
        return default
    value = raw.strip().lower()
    if value not in {"0", "false", "no", "off", "1", "true", "yes", "on"}:
        raise ValueError(f"{ENV_PREFIX + name} 必须为布尔值")
    return value in {"1", "true", "yes", "on"}


@dataclass(frozen=True)
class WorkerConfig:
    """一次 worker 运行使用的全部路径与限额。"""

    root: Path
    source_repo: Path
    base_ref: str = DEFAULT_BASE_REF
    test_command: str = DEFAULT_TEST_COMMAND
    allowed_requesters: tuple[str, ...] = DEFAULT_ALLOWED_REQUESTERS
    # 开发代理单次任务的墙钟上限，超过即按超时收割进程组。
    agent_timeout_sec: int = 1800
    # worker 独立复跑测试的上限。
    test_timeout_sec: int = 1800
    # 传给 dsh 的 bash 工具单次超时（毫秒），默认 60000 跑不完测试。
    bash_timeout_ms: int = 900_000
    # 克隆与 venv 这类准备动作的上限。
    prepare_timeout_sec: int = 600
    pip_index_url: str = DEFAULT_PIP_INDEX
    agent_path: str = DEFAULT_AGENT_PATH
    # 是否用 bwrap 包一层。只在没有 bwrap 的环境（例如 Windows 上跑单元测试）关掉。
    use_sandbox: bool = True
    # 建 venv 用的系统解释器。
    system_python: str = "python3"
    max_log_bytes: int = 8 * 1024 * 1024
    max_patch_bytes: int = 4 * 1024 * 1024
    agent_idle_timeout_sec: int = 300
    # 会话上下文超过该 token 数即压缩；保留最近若干 token 原文，其余改为摘要。
    compact_threshold_tokens: int = 128_000
    compact_retain_tokens: int = 32_000
    # 线上工作流目录与加载器；为 None 时不同步线上基线，也不自动发布。
    live_workflows: Path | None = None
    live_loader: Path | None = None

    # ----- 派生路径 -----

    @property
    def auto_release(self) -> bool:
        return self.live_workflows is not None and self.live_loader is not None

    @property
    def releases_dir(self) -> Path:
        """自动发布记录，与维护者部署脚本的 releases/ 分开。"""
        return self.root / "workflow-releases"

    @property
    def release_lock_path(self) -> Path:
        return self.root / "release.lock"

    @property
    def effective_agent_idle_timeout_sec(self) -> int:
        """静默工具至少有完整的执行期限，另留 30 秒传递结束事件。"""
        return max(self.agent_idle_timeout_sec, (self.bash_timeout_ms + 999) // 1000 + 30)

    @property
    def dsh_root(self) -> Path:
        """dsh 的 npm 安装目录。"""
        return self.root / "dsh"

    @property
    def dsh_bin(self) -> Path:
        """必须用绝对路径调用，避免命中 Windows 全局旧版。"""
        return self.dsh_root / "node_modules" / ".bin" / "dsh"

    @property
    def dsh_home(self) -> Path:
        """DSH_HOME：profiles、会话日志与 .env（只含 DeepSeek 密钥）都在这里。"""
        return self.root / "home"

    @property
    def tasks_dir(self) -> Path:
        return self.root / "tasks"

    @property
    def lock_path(self) -> Path:
        """并发 1 的锁文件。"""
        return self.root / "worker.lock"

    @property
    def gitconfig_path(self) -> Path:
        """worker 专用 gitconfig，不改 qqbot 的全局配置。"""
        return self.root / "gitconfig"

    @property
    def base_venv(self) -> Path:
        """跑测试用的共享基础 venv，位于工作副本之外，代理无写权限。"""
        return self.root / "venv-base"

    @property
    def base_python(self) -> Path:
        return self.base_venv / "bin" / "python"

    def validate(self) -> None:
        """开发基座独立于源码和生产目录，不允许覆盖已有运行树。"""
        root, source = self.root.resolve(), self.source_repo.resolve()
        production = Path('/home/qqbot/QQbot').resolve()
        if (root == source or root.is_relative_to(source) or source.is_relative_to(root)
                or root == production or root.is_relative_to(production)
                or production.is_relative_to(root) or source.is_relative_to(production)):
            raise ValueError('开发目录必须独立于源码和生产目录')
        if not self.base_ref.strip() or '\n' in self.base_ref:
            raise ValueError('base_ref 不能为空或包含换行')
        if not self.allowed_requesters:
            raise ValueError('管理员名单不能为空')
        if not 0 < self.compact_retain_tokens < self.compact_threshold_tokens < 1_000_000:
            raise ValueError('压缩阈值须满足 0 < 保留 < 阈值 < 上下文窗口')

    def requirements_path(self) -> Path:
        return self.source_repo / "requirements.txt"

    @classmethod
    def from_env(cls, environ: dict[str, str] | None = None) -> "WorkerConfig":
        env = dict(os.environ if environ is None else environ)
        requesters = env.get(ENV_PREFIX + "ALLOWED_REQUESTERS")
        allowed = (
            tuple(item.strip() for item in requesters.split(",") if item.strip())
            if requesters
            else DEFAULT_ALLOWED_REQUESTERS
        )
        return cls(
            root=Path(env.get(ENV_PREFIX + "ROOT", DEFAULT_ROOT)),
            source_repo=Path(env.get(ENV_PREFIX + "SOURCE_REPO", DEFAULT_SOURCE_REPO)),
            base_ref=env.get(ENV_PREFIX + "BASE_REF", DEFAULT_BASE_REF),
            test_command=env.get(ENV_PREFIX + "TEST_COMMAND", DEFAULT_TEST_COMMAND),
            allowed_requesters=allowed,
            agent_timeout_sec=_env_int(env, "AGENT_TIMEOUT", 1800),
            test_timeout_sec=_env_int(env, "TEST_TIMEOUT", 1800),
            bash_timeout_ms=_env_int(env, "BASH_TIMEOUT_MS", 900_000),
            prepare_timeout_sec=_env_int(env, "PREPARE_TIMEOUT", 600),
            pip_index_url=env.get(ENV_PREFIX + "PIP_INDEX", DEFAULT_PIP_INDEX),
            agent_path=env.get(ENV_PREFIX + "AGENT_PATH", DEFAULT_AGENT_PATH),
            use_sandbox=_env_bool(env, "SANDBOX", True),
            system_python=env.get(ENV_PREFIX + "SYSTEM_PYTHON", "python3"),
            max_log_bytes=_env_int(env, "MAX_LOG_BYTES", 8 * 1024 * 1024),
            max_patch_bytes=_env_int(env, "MAX_PATCH_BYTES", 4 * 1024 * 1024),
            agent_idle_timeout_sec=_env_int(env, "AGENT_IDLE_TIMEOUT", 300),
            compact_threshold_tokens=_env_int(env, "COMPACT_THRESHOLD_TOKENS", 128_000),
            compact_retain_tokens=_env_int(env, "COMPACT_RETAIN_TOKENS", 32_000),
            live_workflows=_env_path(env, "LIVE_WORKFLOWS", DEFAULT_LIVE_WORKFLOWS),
            live_loader=_env_path(env, "LIVE_LOADER", DEFAULT_LIVE_LOADER),
        )

    def is_allowed(self, requester: str) -> bool:
        return requester in self.allowed_requesters

    def posix(self, path: Path) -> str:
        """给 bwrap / dsh 用的 POSIX 路径字符串。

        单元测试会在 Windows 上构造命令行做断言，这里统一成正斜杠，
        保证断言与 WSL 内的真实命令一致。
        """
        text = str(path).replace("\\", "/")
        return str(PurePosixPath(text))

"""KKBK 新引擎的静态参数。

门控默认值取自 gate-replay 回放实验（点名 + 追踪 + 问题计时 + 3% 随机探索）；
夜间词表只留接口，不作为触发器。
"""

from dataclasses import dataclass, field
from typing import FrozenSet, Tuple


@dataclass(frozen=True)
class GateConfig:
    """会话门控状态机参数。"""

    debounce_seconds: float = 3.0
    """距最近一条消息超过该秒数，视为一批消息结束。"""
    track_seconds: float = 180.0
    """追踪状态下无新理由多久后退回静态扫描。"""
    track_max_silent_looks: int = 4
    """追踪中模型连续 silent 达到该次数后退出（回放实验的空转退出）。"""
    track_leave_looks: int = 2
    """模型连续 silent 且标记 leave 达到该次数后退出追踪。"""
    question_wait_seconds: float = 60.0
    """问题多少秒无人接后让模型看一眼。"""
    question_min_chars: int = 4
    """短于该字数的消息不当作问题。"""
    explore_probability: float = 0.03
    """静态扫描下每批消息随机看一眼的概率。"""


@dataclass(frozen=True)
class IdentityConfig:
    """身份表：本 bot、操作员、白名单群和已标记机器人。"""

    bot_user_id: str = "3976405558"
    bot_display_name: str = "KKBK"
    wake_words: Tuple[str, ...] = ("kkbk", "伊蕾娜", "娜娜")
    """唤醒名，匹配时忽略大小写。"""
    operator_user_ids: FrozenSet[str] = frozenset({"1020050112"})
    """重工具与管理授权只看实时发送者 QQ 号，永不来自记忆、摘要或聊天内容。"""
    teacher_user_id: str = ""
    """留空时使用唯一维护者；多维护者场景可明确指定其中一人为方法教师。"""
    group_whitelist: FrozenSet[str] = frozenset({"1022382380", "894179464", "427204073"})
    blocked_robot_user_ids: FrozenSet[str] = frozenset({"3889590033"})
    """OneBot 成员资料 is_robot=true 的账号；运行期由成员查询刷新，这里是已知初值。"""

    @property
    def method_teacher_id(self) -> str | None:
        """教学身份与维护者配置同源，不自动沿用旧账号。"""
        if self.teacher_user_id:
            if self.teacher_user_id not in self.operator_user_ids:
                raise ValueError("方法教师必须是已配置维护者")
            return self.teacher_user_id
        if len(self.operator_user_ids) == 1:
            return next(iter(self.operator_user_ids))
        return None


@dataclass(frozen=True)
class LoopBreakerConfig:
    """不依赖 bot 识别的防死循环：与同一账号来回过多且无第三人插话时冷却。"""

    window_seconds: float = 600.0
    max_rounds: int = 10
    """窗口内来回超过该轮数进入冷却（一轮 = KKBK 与对方各说一次）。"""
    cooldown_seconds: float = 600.0


@dataclass(frozen=True)
class ReplyLimits:
    """reply 动作的宿主侧上限。"""

    max_chars: int = 1500
    max_sends_per_window: int = 6
    send_window_seconds: float = 60.0


@dataclass(frozen=True)
class ForwardLimits:
    """合并转发单条输入上限（不是预算）。"""

    max_depth: int = 2
    max_nodes: int = 40
    max_chars: int = 1500
    max_images: int = 4
    """L2 单条转发最多识别的图片数。"""
    preview_chars: int = 40
    """L0 结构摘要里保留的开头字数。"""


@dataclass(frozen=True)
class HistoryConfig:
    """轻路径消息历史：只追加不改写，超长时整段滚动。"""

    max_entries: int = 80
    keep_after_roll: int = 40


@dataclass(frozen=True)
class TaskBudget:
    """单轮工具预算；频率限流另外计算，避免预算扩大后突发刷接口。"""

    max_tool_rounds: int = 20
    max_wait_seconds: float = 240.0


@dataclass(frozen=True)
class EngineConfig:
    """引擎全部静态参数。"""

    gate: GateConfig = field(default_factory=GateConfig)
    identity: IdentityConfig = field(default_factory=IdentityConfig)
    loop_breaker: LoopBreakerConfig = field(default_factory=LoopBreakerConfig)
    reply: ReplyLimits = field(default_factory=ReplyLimits)
    forward: ForwardLimits = field(default_factory=ForwardLimits)
    history: HistoryConfig = field(default_factory=HistoryConfig)
    task: TaskBudget = field(default_factory=TaskBudget)
    plugin_tools: FrozenSet[str] = frozenset(
        {"chatgpt_image", "web_search", "read_webpage", "analyze_task_image", "image_reverse_search"}
    )
    """交给任务层的插件工具，同时沿用既有插件工具限流。"""

"""把搜索报告格式化为统一的中文回复。"""

from typing import List

from .models import EngineAttempt, EngineStatus, SearchReport, SearchResult

_STATUS_LABELS = {
    EngineStatus.HIT: "命中/可用",
    EngineStatus.LOW_SCORE: "可用/低分",
    EngineStatus.NO_RESULT: "无结果",
    EngineStatus.TIMEOUT: "超时",
    EngineStatus.ERROR: "失败",
    EngineStatus.RATE_LIMITED: "限流",
    EngineStatus.SKIPPED: "跳过",
}

_MAX_FIELD_LENGTH = 300


def format_report(report: SearchReport) -> str:
    """生成用户可见的图片反查结果。

    命中时输出最高分结果的标题、作者、来源和相似度；否则输出低分候选或
    明确的无命中说明，并始终附带全部引擎尝试记录。
    """

    lines: List[str] = [f"🔎 图片反查结果（{report.mode_label}）"]
    if report.hit is not None:
        lines.extend(_format_hit(report.hit))
    elif report.low_results:
        lines.append(f"⚠️ 没有结果达到命中阈值 {report.threshold:.1f}%，以下为低分候选：")
        lines.extend(_format_low_results(report.low_results))
    else:
        lines.append(f"❌ 所有引擎均未命中（命中阈值 {report.threshold:.1f}%）。")

    lines.append("")
    lines.append("引擎尝试记录：")
    if report.attempts:
        lines.extend(_format_attempts(report))
    else:
        lines.append("- 未执行任何引擎")
    return "\n".join(lines)


def format_usage() -> str:
    """返回群内命令用法。"""

    return (
        "🖼️ 图片反查用法：\n"
        "1. 发送图片并附带文字 `/搜图`：按二次元插画链路搜索。\n"
        "2. 发送番剧截图并附带 `/搜番`：优先使用 trace.moe。\n"
        "3. 也可以使用 `/搜图 番剧` 临时指定番剧模式。"
    )


def _format_hit(result: SearchResult) -> List[str]:
    """格式化命中结果。"""

    lines = [
        f"✅ 命中引擎：{result.engine_label}",
        f"标题：{_field(result.title) or '（未提供）'}",
        f"作者/画师：{_field(result.author) or '未知'}",
        f"来源链接：{_source_url(result.source_url)}",
        f"相似度：{result.similarity_label}",
    ]
    if result.detail:
        lines.append(f"备注：{_field(result.detail)}")
    return lines


def _format_low_results(results: List[SearchResult]) -> List[str]:
    """格式化低分候选列表。"""

    lines: List[str] = []
    for index, result in enumerate(results, start=1):
        lines.append(
            f"{index}. [{result.engine_label} · {_short_similarity(result)}] {_field(result.title) or '（未提供标题）'}"
        )
        lines.append(f"   作者/画师：{_field(result.author) or '未知'}；来源：{_source_url(result.source_url)}")
    return lines


def _format_attempts(report: SearchReport) -> List[str]:
    """格式化全部引擎尝试记录。"""

    lines: List[str] = []
    for attempt in report.attempts:
        status_label = _status_label(attempt)
        parts = [f"{attempt.elapsed_ms / 1000:.1f}s"]
        if attempt.best_score is not None and attempt.status != EngineStatus.HIT:
            parts.append(f"最高 {attempt.best_score:.1f}%")
        if attempt.reason:
            parts.append(attempt.reason)
        suffix = f"（{'，'.join(parts)}）"
        lines.append(f"- {attempt.engine_label}：{status_label}{suffix}")
    return lines


def _status_label(attempt: EngineAttempt) -> str:
    """按来源状态生成标签；缺 key 的跳过要明确写出原因类别。"""

    if attempt.status == EngineStatus.SKIPPED and "key" in attempt.reason.lower():
        return "缺 key 跳过"
    return _STATUS_LABELS.get(attempt.status, attempt.status)


def _short_similarity(result: SearchResult) -> str:
    """低分列表中的短相似度文本。"""

    if result.similarity is None:
        return "相似度未知"
    return f"{result.similarity:.1f}%"


def _source_url(value: object) -> str:
    """只展示 HTTP(S) 来源链接。"""

    text = _field(value)
    if text.startswith(("http://", "https://")):
        return text
    return "（未提供）"


def _field(value: object) -> str:
    """清理单个展示字段并限制长度。"""

    text = " ".join(str(value or "").split())
    if len(text) > _MAX_FIELD_LENGTH:
        text = text[: _MAX_FIELD_LENGTH - 1] + "…"
    return text

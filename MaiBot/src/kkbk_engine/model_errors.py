"""沿结构化异常链识别模型错误，不以消息中的数字或词语猜测状态。"""

from src.llm_models.exceptions import (
    EmptyResponseException,
    NetworkConnectionError,
    ReqAbortException,
    RespParseException,
)

from .actions import ActionError

QUOTA_CODES = frozenset(
    {"insufficient_quota", "insufficient_balance", "billing_hard_limit_reached", "credit_balance_exhausted"}
)


class ModelOutputError(RuntimeError):
    """响应已返回但不能生成动作；只携带诊断类别，不包含思考或原始响应。"""

    def __init__(self, kind: str):
        super().__init__(kind)
        self.kind = kind


def output_metadata(result) -> dict:
    """只提取结束原因和字段长度，保留空输出证据且不落盘隐藏思考或拒绝正文。"""
    raw = getattr(result, "provider_response", None) or {}
    choices = raw.get("choices") or []
    choice = choices[0] if choices and isinstance(choices[0], dict) else {}
    message = choice.get("message") or {}
    finish = choice.get("finish_reason")
    # 日志只接受已知枚举，避免将提供方异常字段当作任意文本记录。
    known = {"stop", "length", "tool_calls", "function_call", "content_filter"}
    return {
        "finish_reason": finish if finish in known else "unknown",
        "refusal_present": bool(message.get("refusal")),
        "content_chars": len(result.response),
        "reasoning_chars": len(getattr(result, "reasoning", "")),
        "tool_call_count": len(result.tool_calls or []),
        "raw_content_kind": (
            message.get("content_kind") if message.get("content_kind") in {"none", "text", "blocks", "other"} else None
        ),
        "raw_content_chars": message.get("content_chars") if isinstance(message.get("content_chars"), int) else None,
        "raw_reasoning_chars": message.get("reasoning_chars") if isinstance(message.get("reasoning_chars"), int) else None,
    }


def check_model_output(result, metadata: dict) -> None:
    """明确拒绝不补试；无正文且无工具才是空输出，不能将它当成 silent。"""
    if metadata["refusal_present"] or metadata["finish_reason"] == "content_filter":
        raise ModelOutputError("refused")
    if not result.response.strip() and not result.tool_calls:
        raise ModelOutputError("empty_output")


def error_kind(exc: Exception) -> str:
    """读取宿主包装异常及 SDK 原始异常，额度和鉴权错误优先于临时网络错误。"""
    pending, seen = [exc], set()
    transient = False
    denied = False
    empty = False
    while pending:
        current = pending.pop()
        if id(current) in seen:
            continue
        seen.add(id(current))
        if isinstance(current, ReqAbortException):
            return "aborted"
        if isinstance(current, ModelOutputError):
            return current.kind
        empty |= isinstance(current, EmptyResponseException)
        status = getattr(current, "status_code", None)
        response = getattr(current, "response", None)
        if status is None and response is not None:
            status = getattr(response, "status_code", None)
        codes = [getattr(current, "code", None)]
        for name in ("body", "ext_info"):
            data = getattr(current, name, None)
            if isinstance(data, dict):
                error = data.get("error", data)
                if isinstance(error, dict):
                    codes.extend(value for value in (error.get("code"), error.get("type")) if isinstance(value, str))
        if status == 402 or any(isinstance(code, str) and code in QUOTA_CODES for code in codes):
            return "quota"
        if status in (401, 403):
            denied = True
        elif isinstance(status, int) and 400 <= status < 500 and status != 429:
            denied = True
        transient |= status == 429 or (isinstance(status, int) and status >= 500)
        transient |= isinstance(
            current,
            (ActionError, EmptyResponseException, RespParseException, NetworkConnectionError, TimeoutError, OSError),
        )
        for name in ("original_exception", "__cause__", "__context__"):
            nested = getattr(current, name, None)
            if isinstance(nested, Exception):
                pending.append(nested)
    return "request" if denied else "empty_output" if empty else "transient" if transient else "unknown"


def retryable(exc: Exception) -> bool:
    return error_kind(exc) in {"transient", "empty_output"}


def failure_text(exc: Exception) -> str:
    kind = error_kind(exc)
    if kind == "quota":
        return "模型额度或余额不足，本轮已停止模型请求；已执行的步骤不会自动重跑。"
    if kind == "empty_output":
        return "模型连续返回空正文，本轮未取得可用回答；这不是主动选择不回复，已执行的步骤不会自动重跑。"
    if kind == "refused":
        return "模型返回了明确拒绝或内容过滤标记，本轮已停止；已执行的步骤不会自动重跑。"
    return "这次任务执行出错，没能完成；已执行的步骤不会自动重跑。"

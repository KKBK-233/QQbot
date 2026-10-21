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


def error_kind(exc: Exception) -> str:
    """读取宿主包装异常及 SDK 原始异常，额度和鉴权错误优先于临时网络错误。"""
    pending, seen = [exc], set()
    transient = False
    denied = False
    while pending:
        current = pending.pop()
        if id(current) in seen:
            continue
        seen.add(id(current))
        if isinstance(current, ReqAbortException):
            return "aborted"
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
    return "request" if denied else "transient" if transient else "unknown"


def retryable(exc: Exception) -> bool:
    return error_kind(exc) == "transient"


def failure_text(exc: Exception) -> str:
    if error_kind(exc) == "quota":
        return "模型额度或余额不足，本轮已停止模型请求；已执行的步骤不会自动重跑。"
    return "这次任务执行出错，没能完成；已执行的步骤不会自动重跑。"

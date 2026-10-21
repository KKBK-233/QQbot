"""引擎错误类型。

错误消息只允许包含安全摘要，不得包含 API key、图片路径、URL 查询参数或响应正文。
"""


class EngineError(Exception):
    """引擎调用失败的基类。"""


class EngineUnavailable(EngineError):
    """引擎因缺少依赖或凭据而无法调用。"""


class EngineTimeout(EngineError):
    """引擎请求超时。"""


class EngineRateLimited(EngineError):
    """引擎触发了限流。"""


class EngineRequestError(EngineError):
    """引擎请求或响应解析失败。"""

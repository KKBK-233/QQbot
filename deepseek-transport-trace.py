"""DeepSeek专用本机通道与传输诊断，只记录阶段、耗时和字节计数。"""
import asyncio
import time
import uuid

import httpx
from openai import DefaultAsyncHttpxClient


class RequestTrace:
    """单次HTTP尝试的有界元数据，结束或取消后清理等待提示定时器。"""
    def __init__(self, report, interval):
        self.report = report
        self.interval = interval
        self.started = time.monotonic()
        self.ident = uuid.uuid4().hex[:10]
        self.phase = '等待响应头'
        self.status = None
        self.encoding = 'identity'
        self.body_bytes = self.blank_bytes = 0
        self.first_body = self.first_content = None
        self.finished = False
        self.timer = asyncio.get_running_loop().call_later(interval, self.waiting)

    def emit(self, outcome):
        """不记录URL、请求头、正文或异常消息，日志故障也不影响请求。"""
        blank = self.blank_bytes if self.encoding == 'identity' else '压缩内容未判定'
        try:
            self.report(f'DeepSeek传输 id={self.ident} 状态={outcome} '
                        f'耗时={time.monotonic() - self.started:.2f}s 阶段={self.phase} '
                        f'HTTP={self.status} 编码={self.encoding} 正文原始字节={self.body_bytes} '
                        f'空白字节={blank} 首正文={self.first_body} 首非空白={self.first_content}')
        except Exception:
            pass

    def waiting(self):
        if not self.finished:
            self.emit('等待中')
            self.timer = asyncio.get_running_loop().call_later(self.interval, self.waiting)

    async def event(self, name, info):
        # httpcore事件名称来自客户端代码；info可能包含凭据，因此完全不读取。
        if name.startswith(('connection.', 'proxy.', 'http11.', 'http2.')):
            self.phase = name

    def body(self, chunk):
        now = round(time.monotonic() - self.started, 3)
        self.body_bytes += len(chunk)
        if chunk and self.first_body is None:
            self.first_body = now
        if self.encoding == 'identity':
            if not chunk.strip():
                self.blank_bytes += len(chunk)
            elif self.first_content is None:
                self.first_content = now

    def finish(self, outcome):
        if not self.finished:
            self.finished = True
            self.timer.cancel()
            self.emit(outcome)


class TraceStream(httpx.AsyncByteStream):
    """原样转交响应字节，不缓存、解析或修改模型结果。"""
    def __init__(self, original, state):
        self.original, self.state = original, state

    async def __aiter__(self):
        try:
            async for chunk in self.original:
                self.state.body(chunk)
                yield chunk
        except BaseException as error:
            self.state.finish(type(error).__name__)
            raise
        else:
            self.state.finish('完成')

    async def aclose(self):
        try:
            await self.original.aclose()
        finally:
            self.state.finish('连接关闭')


class TracedDeepSeekClient(DefaultAsyncHttpxClient):
    """保留SDK网络策略，仅限制DeepSeek建连等待，生成内容的读取预算不变。"""
    def __init__(self, *, report, diagnostic_interval=30, **kwargs):
        super().__init__(**kwargs)
        self.report, self.diagnostic_interval = report, diagnostic_interval

    async def send(self, request, *, stream=False, **kwargs):
        # 只观察官方DeepSeek主机，其他请求完全使用原实现。
        if request.url.host != 'api.deepseek.com':
            return await super().send(request, stream=stream, **kwargs)
        # SDK会把提供商的标量超时写回每次请求，必须在此处只缩短connect预算。
        # 连接失败交给原有SDK重试；不延长用户已有的更短超时，也不改变直连/代理选择。
        timeout = request.extensions.get('timeout')
        if isinstance(timeout, dict):
            connect = timeout.get('connect')
            if connect is None or connect > 10:
                request.extensions['timeout'] = {**timeout, 'connect': 10.0}
        state = RequestTrace(self.report, self.diagnostic_interval)
        previous_trace = request.extensions.get('trace')
        async def trace(name, info):
            await state.event(name, info)
            if previous_trace is not None:
                await previous_trace(name, info)
        request.extensions['trace'] = trace
        response = None
        try:
            response = await super().send(request, stream=True, **kwargs)
            state.status = response.status_code
            encoding = response.headers.get('content-encoding', 'identity')
            state.encoding = encoding if encoding in ('identity', 'gzip', 'deflate', 'br', 'zstd') else 'other'
            state.phase = '读取响应正文'
            if response.is_stream_consumed:
                # 测试传输或响应Hook可能已缓存正文，此时aread不会再次遍历字节流。
                state.phase = '已有缓存正文'
                state.encoding = 'identity'
                state.body(response.content)
                state.finish('完成')
            else:
                response.stream = TraceStream(response.stream, state)
            if not stream:
                await response.aread()
            return response
        except BaseException as error:
            state.finish(type(error).__name__)
            if response is not None:
                await response.aclose()
            raise
        finally:
            # SDK重试可能复用Request，不能把旧诊断回调堆叠到下一次尝试。
            if previous_trace is None:
                request.extensions.pop('trace', None)
            else:
                request.extensions['trace'] = previous_trace


def create_client(timeout):
    """仅DeepSeek经本机通道由Windows发起连接，不使用外部HTTP代理节点。"""
    from src.common.logger import get_logger
    return TracedDeepSeekClient(report=get_logger('deepseek_transport').info, timeout=timeout,
                               proxy='http://127.0.0.1:2082', trust_env=False)

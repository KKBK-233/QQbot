"""常驻桥接内直接请求 Codex 后端，复用已核验协议，不启动任务子进程。"""
import json
import os
from pathlib import Path
import re
import time
from types import SimpleNamespace
import urllib.error
import urllib.parse
import urllib.request

from backend_trace import BackendTrace
from image_use_runner import pin_codex_driver


def load_runtime(source, proxy):
    """每个任务有独立函数环境和代理，避免并发任务串诊断或改动全局网络设置。"""
    runtime = {'__name__': 'qqbot_codex_direct', '__file__': str(source)}
    exec(compile(source.read_bytes(), str(source), 'exec'), runtime)
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({'http': proxy, 'https': proxy}))
    # 沿用原来的 SSE 解析、超时和刷新协议，仅在这个函数环境替换 HTTP 打开方式。
    request_module = SimpleNamespace(**{**vars(urllib.request), 'urlopen': opener.open})
    runtime['urllib'] = SimpleNamespace(request=request_module, parse=urllib.parse, error=urllib.error)
    return runtime


def installed_version():
    """读取实际 Windows npm 安装版本，不运行 CLI，也不使用旧版本缓存。"""
    package = Path(os.environ['APPDATA']) / 'npm/node_modules/@openai/codex/package.json'
    version = json.loads(package.read_text(encoding='utf-8')).get('version', '')
    if not re.fullmatch(r'\d+\.\d+\.\d+', version):
        raise ValueError('已安装 Codex 版本无法识别')
    return version


def generate(source, prompt, refs, model, background, proxy, concurrency, diagnostic_path):
    """与旧通道使用相同输入和额度；结果与诊断直接返回，不经 stderr 或退出码转译。"""
    trace = BackendTrace()
    image = None
    error_data = b''
    try:
        runtime = load_runtime(source, proxy)
        pin_codex_driver(runtime, model)
        runtime['_detect_codex_version'] = installed_version
        # 跨进程账号槽和桥接线程池均使用管理员的原有并发上限。
        runtime['_backend_limit'] = lambda kind, default: concurrency if kind == 'codex' else default
        original_stream = runtime['_stream']

        def observed_stream(url, headers, body, deadline, stall_timeout):
            """只观测公共阶段及脱敏终态；各任务独立闭包，不记录推理或输入正文。"""
            trace.begin(body)
            try:
                for event in original_stream(url, headers, body, deadline, stall_timeout):
                    trace.event(event)
                    yield event
            except BaseException as error:
                trace.failure(error)
                raise

        runtime['_stream'] = observed_stream
        args = SimpleNamespace(gen_prompt=prompt, model=model, size='auto', format='png',
                               timeout=600, background=background)
        image, _ = runtime['run_codex'](args, False, 120, time.monotonic(), [str(p) for p in refs])
    except (Exception, SystemExit) as error:
        if 'exception_type' not in trace.data:
            trace.failure(error)
        # 原文仅用于本机固定错误分类，调用方不得直接输出到群或状态接口。
        error_data = str(error).encode('utf-8', errors='replace')[:8192]
    finally:
        trace.data['diagnostic_written'] = trace.diagnostic.save(diagnostic_path)
    return SimpleNamespace(image=image, trace=trace.data, error_data=error_data)

"""Windows 兼容入口：用临时文件接收 CLI 输出，避免后台进程继承管道造成永久等待。"""

import os
import json
from pathlib import Path
import re
import runpy
import subprocess
import sys
import tempfile
from backend_trace import BackendTrace


def pin_codex_driver(runtime, model):
    """固定指定驱动与low思考，禁用不支持时向旧模型静默回退。"""
    runtime['CODEX_MODEL_FALLBACK'] = model
    original_builder = runtime['_build_payload']
    def build_payload(*args, **kwargs):
        payload = original_builder(*args, **kwargs)
        payload['model'] = model
        payload['reasoning'] = {**payload.get('reasoning', {}), 'effort': 'low'}
        return payload
    runtime['_build_payload'] = build_payload


def main():
    source = sys.argv.pop(1)
    sys.argv[0] = source
    namespace = runpy.run_path(source, run_name="image_use_bounded_runner")
    gateway_error = namespace["GatewayError"]

    def browser_command(ab, *args, session, timeout, profile=None, input_text=None):
        """保留上游参数逻辑，改为 UTF-8 和可限时等待的文件输出，不开放额外浏览器命令。"""
        command = [ab]
        if profile:
            command.extend(["--profile", profile])
        command.extend([*args, "--session", session])
        flags = {"creationflags": subprocess.CREATE_NO_WINDOW} if os.name == "nt" else {}
        try:
            with tempfile.TemporaryFile() as stdout, tempfile.TemporaryFile() as stderr:
                result = subprocess.run(command, input=input_text.encode("utf-8") if input_text is not None else None,
                                        stdout=stdout, stderr=stderr, timeout=max(5.0, timeout), **flags)
                stdout.seek(0)
                stderr.seek(0)
                output = stdout.read().decode("utf-8", errors="replace")
                error = stderr.read().decode("utf-8", errors="replace")
            if result.returncode:
                tail = (error or output).strip().splitlines()[-3:]
                raise gateway_error(f"chrome-use {args[0]} failed (exit {result.returncode}): {' / '.join(tail)[:300]}")
            return output
        except subprocess.TimeoutExpired:
            raise gateway_error("浏览器命令超时；不自动重发") from None
        except FileNotFoundError:
            raise gateway_error("chrome-use 程序不存在") from None

    namespace["main"].__globals__["_ab"] = browser_command
    # 当前中文网页的发送按钮没有 type=submit，使用已观察到的明确标签，并限定在输入框所在 form。
    runtime = namespace["main"].__globals__
    # 显式指定驱动模型时禁止上游静默退回旧型号；不支持时直接返回失败。
    if '--backend' in sys.argv and sys.argv[sys.argv.index('--backend') + 1] == 'codex' and '--model' in sys.argv:
        pin_codex_driver(runtime, sys.argv[sys.argv.index('--model') + 1])
    trace = BackendTrace()
    original_stream = runtime['_stream']
    def traced_stream(url, headers, body, deadline, stall_timeout):
        """只观察固定元数据，响应字节、生成和重试行为沿用上游实现。"""
        trace.begin(body)
        try:
            for event in original_stream(url, headers, body, deadline, stall_timeout):
                trace.event(event)
                yield event
        except BaseException as error:
            trace.failure(error)
            raise
    runtime['_stream'] = traced_stream
    original_version = runtime["_detect_codex_version"]

    def installed_codex_version():
        """读取已安装 CLI 的真实版本，避免过期 latest_version 缓存导致后端误判。"""
        if os.name == "nt" and os.environ.get("APPDATA"):
            package = Path(os.environ["APPDATA"]) / "npm/node_modules/@openai/codex/package.json"
            try:
                version = json.loads(package.read_text(encoding="utf-8")).get("version", "")
                if re.fullmatch(r"\d+\.\d+\.\d+", version):
                    return version
            except (OSError, ValueError):
                pass
        return original_version()

    runtime["_detect_codex_version"] = installed_codex_version
    localized_send = 'button[aria-label="发送"]'
    runtime["WEB_SEND_SEL"] += ", " + localized_send
    runtime["WEB_SEND_CLICK_SEL"] += ', form:has(div.ProseMirror[contenteditable="true"]) ' + localized_send
    try:
        return namespace["main"]()
    except BaseException as error:
        # 流中的HTTP/SSL错误比外层SystemExit更具体，保留已有分类。
        if 'exception_type' not in trace.data:
            trace.failure(error)
        raise
    finally:
        if '--backend' in sys.argv and sys.argv[sys.argv.index('--backend') + 1] == 'codex':
            # 路径只由管理员桥接设置；诊断正文不打印到工具返回或公共日志。
            diagnostic_path = os.environ.get('QQBOT_IMAGE_DIAGNOSTIC_PATH')
            if diagnostic_path:
                trace.data['diagnostic_written'] = trace.diagnostic.save(diagnostic_path)
            print(trace.line(), file=sys.stderr)


if __name__ == "__main__":
    sys.exit(main())

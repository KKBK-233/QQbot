"""源码发布专用的幂等服务操作与只读健康检查，不操作 QQ 登录进程。"""

from pathlib import Path
import argparse
import json
import re
import subprocess
import time
import urllib.request


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("operation", choices=["stop", "start", "health"])
    parser.add_argument("service", choices=["maibot", "dev-worker"])
    parser.add_argument("--config", default="/home/qqbot/QQbot/supervisord.conf")
    parser.add_argument("--url", default="http://127.0.0.1:8001/api/webui/health")
    args = parser.parse_args(argv)
    command = ["supervisorctl", "-c", args.config]
    log = Path("/home/qqbot/QQbot/logs/maibot.log")
    marker = Path.cwd() / "maibot-log-offset.json"

    def state():
        raw = (
            subprocess.run(
                [*command, "status", args.service], capture_output=True, timeout=10
            )
            .stdout.decode()
            .split()
        )
        return raw[1] if len(raw) > 1 else "UNKNOWN"

    if args.operation == "stop":
        if state() not in {"STOPPED", "EXITED", "FATAL"}:
            subprocess.run(
                [*command, "stop", args.service],
                check=True,
                capture_output=True,
                timeout=60,
            )
    elif args.operation == "start":
        if state() != "RUNNING":
            if args.service == "maibot":
                marker.write_text(
                    json.dumps({"offset": log.stat().st_size if log.exists() else 0})
                )
            subprocess.run(
                [*command, "start", args.service],
                check=True,
                capture_output=True,
                timeout=60,
            )
    else:
        deadline = time.monotonic() + 45
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
        while True:
            try:
                if state() != "RUNNING":
                    raise RuntimeError("服务未运行")
                if args.service == "maibot":
                    with opener.open(args.url, timeout=3) as response:
                        if json.load(response).get("status") != "healthy":
                            raise RuntimeError("HTTP 健康检查未通过")
                    if marker.exists():
                        with log.open("rb") as stream:
                            offset = json.loads(marker.read_text())["offset"]
                            stream.seek(offset if log.stat().st_size >= offset else 0)
                            lines = stream.read().decode("utf-8", "replace")
                        if any(
                            "已加载=" in line and re.search(r"失败=[1-9]\d*", line)
                            for line in lines.splitlines()
                        ):
                            raise RuntimeError("插件加载存在失败")
                        if "全部系统初始化完成" not in lines or "已加载=" not in lines:
                            raise RuntimeError("初始化或插件加载尚未完成")
                else:
                    if not Path("/home/qqbot/dev-harness/run/control.sock").is_socket():
                        raise RuntimeError("开发控制 socket 尚未恢复")
                break
            except Exception:
                if time.monotonic() >= deadline:
                    raise
                time.sleep(1)
    print(
        json.dumps(
            {"service": args.service, "operation": args.operation, "success": True}
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

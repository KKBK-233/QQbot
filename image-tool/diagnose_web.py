"""只读检测上游实际的浏览器调用与输入框识别，禁止提交或读取账号令牌。"""

import json
import os
from pathlib import Path
import runpy
import time

folder = Path(__file__).resolve().parent
config = json.loads((folder / "config.local.json").read_text(encoding="utf-8"))
os.environ["PATH"] = str(Path(config["chrome_use"]).parent) + os.pathsep + os.environ.get("PATH", "")
upstream = runpy.run_path(config["image_use"], run_name="image_use_diagnostic")
started = time.monotonic()
try:
    result = upstream["_ab_eval"](config["chrome_use"],
        upstream["_JS_COMPOSER"] % json.dumps(upstream["WEB_COMPOSER_SEL"]),
        session="qqbot-image-only", timeout=20)
    print(json.dumps({"composer_probe": result, "seconds": round(time.monotonic() - started, 2)}, ensure_ascii=True))
except Exception as error:
    print(json.dumps({"type": type(error).__name__, "seconds": round(time.monotonic() - started, 2),
                      "error": str(error)[:350]}, ensure_ascii=True))

"""安装独立匿名网页渲染运行时，不升级 MaiBot 的 Python 依赖或个人浏览器。"""
from pathlib import Path
import os
import subprocess
import venv

ROOT = Path('/home/qqbot/QQbot')
runtime = ROOT / 'run/web-renderer-venv'
venv.EnvBuilder(with_pip=False).create(runtime)
env = dict(os.environ, HTTP_PROXY='http://127.0.0.1:2081', HTTPS_PROXY='http://127.0.0.1:2081',
           PLAYWRIGHT_BROWSERS_PATH=str(ROOT / 'run/web-browsers'))
subprocess.run([str(ROOT / 'MaiBot/.venv/bin/python'), '-m', 'pip', '--python', str(runtime),
                'install', 'playwright==1.63.0'], env=env, check=True)
subprocess.run([str(runtime / 'bin/python'), '-m', 'playwright', 'install', 'chromium', '--only-shell'],
               env=env, check=True)
print('独立匿名 Chromium 运行时安装完成，MaiBot 依赖及个人浏览器配置未修改')

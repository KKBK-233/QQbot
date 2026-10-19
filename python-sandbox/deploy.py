"""部署独立沙箱插件，逐项校验；不覆盖配置，不重启QQ或其他服务。"""

import hashlib
from pathlib import Path
import shutil
import subprocess
import datetime


def patch_context():
    """只为沙箱覆盖模型伪造的身份字段，以真实宿主上下文决定限流范围。"""
    target = Path('/home/qqbot/QQbot/MaiBot/src/plugin_runtime/component_query.py')
    text = target.read_text()
    marker = '        # 沙箱配额身份只信任宿主，不接受模型传入的保留字段。\n'
    if marker in text:
        return
    anchor = '        context_payload = ComponentQueryService._build_tool_context_payload(context)\n'
    if text.count(anchor) != 1:
        raise RuntimeError('宿主源码变化，拒绝不明确的身份补丁')
    replacement = anchor + marker + '''        if entry.plugin_id == "local.qqbot-python-sandbox":
            for key in ("_sandbox_trusted_context", "stream_id", "chat_id", "user_id", "group_id", "platform"):
                payload.pop(key, None)
            payload.update(context_payload)
            payload["_sandbox_trusted_context"] = bool(context_payload.get("stream_id"))
            return payload
'''
    updated = text.replace(anchor, replacement)
    compile(updated, str(target), 'exec')
    backup = target.with_name('component_query.py.sandbox-' + datetime.datetime.now().strftime('%Y%m%d%H%M%S') + '.bak')
    shutil.copyfile(target, backup)
    target.write_text(updated)


def main():
    """先运行实际隔离测试，再安装已验证文件。"""
    source = Path(__file__).resolve().parent
    if not Path('/usr/bin/bwrap').exists():
        raise SystemExit('请在已安装Bubblewrap的WSL内以qqbot用户运行')
    subprocess.run(['/usr/bin/python3', '-m', 'unittest', 'test_sandbox', '-v'], cwd=source, check=True)
    subprocess.run(['/usr/bin/python3', '-m', 'unittest', 'test_budget', '-v'], cwd=source, check=True)
    subprocess.run(['/home/qqbot/QQbot/MaiBot/.venv/bin/python', str(source / 'verify_plugin.py')], check=True)
    patch_context()
    subprocess.run(['/usr/bin/python3', '-m', 'unittest', 'test_context', '-v'], cwd=source, check=True)
    target = Path('/home/qqbot/QQbot/MaiBot/plugins/qqbot-python-sandbox')
    target.mkdir(mode=0o700, exist_ok=True)
    for name in ('_manifest.json', 'plugin.py', 'runner.py', 'worker.py', 'budget.py'):
        shutil.copyfile(source / name, target / name)
        if hashlib.sha256((source / name).read_bytes()).digest() != hashlib.sha256((target / name).read_bytes()).digest():
            raise RuntimeError('部署内容校验失败')
    print('沙箱测试及部署完成，需重载MaiBot以注册run_python。QQ/SnowLuma未操作。')


if __name__ == '__main__':
    main()

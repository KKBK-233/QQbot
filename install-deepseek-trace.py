"""安装DeepSeek元数据诊断，备份并只改兼容客户端的HTTP客户端参数。"""
from datetime import datetime, timezone
from pathlib import Path
import ast
import shutil


def main():
    """严格匹配构造器，备份传输补丁，模型配置与代理环境保持不变。"""
    source = Path('/mnt/d/code/web/QQbot')
    root = Path('/home/qqbot/QQbot/MaiBot/src/llm_models/model_client')
    path = root / 'openai_client.py'
    original = path.read_text(encoding='utf-8')
    marker = '        # QQbot：DeepSeek仅增加无凭据的阶段诊断，保持SDK默认网络策略。'
    changed = original
    if marker not in original:
        old = '        self.client = AsyncOpenAI(\n'
        new = marker + '''
        http_client = None
        from urllib.parse import urlsplit
        if urlsplit(client_config.base_url).hostname == 'api.deepseek.com':
            from .qqbot_deepseek_trace import create_client
            http_client = create_client(api_provider.timeout)
        self.client = AsyncOpenAI(
            http_client=http_client,
'''
        assert original.count(old) == 1, '兼容客户端构造器已变化，未应用补丁'
        changed = original.replace(old, new, 1)
    ast.parse(changed)
    helper = (source / 'deepseek-transport-trace.py').read_text(encoding='utf-8')
    ast.parse(helper)
    if changed != original:
        stamp = datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')
        shutil.copy2(path, path.with_name(path.name + '.before-deepseek-trace-' + stamp))
    helper_path = root / 'qqbot_deepseek_trace.py'
    if helper_path.exists() and helper_path.read_text(encoding='utf-8') != helper:
        stamp = datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')
        shutil.copy2(helper_path, helper_path.with_name(helper_path.name + '.before-connect-budget-' + stamp))
    helper_path.write_text(helper, encoding='utf-8')
    path.write_text(changed, encoding='utf-8')
    print('DeepSeek本机通道与诊断已安装；Windows连接官方API，建连最多10秒，读取预算与重试配置保持。')


if __name__ == '__main__':
    main()

"""启用原生长期记忆及反馈纠错，只改指定开关，备份配置且不输出凭据。"""
from datetime import datetime, timezone
from pathlib import Path
import re
import shutil
import tomllib


def enable(text):
    """保留原有注释和其他配置，群间共享与等待策略沿用当前设置。"""
    changes = [('a_memorix.plugin', 'enabled'), ('a_memorix.integration', 'feedback_correction_enabled')]
    for section, key in changes:
        pattern = r'(^\[' + re.escape(section) + r'\][^\n]*\n)(.*?)(?=^\[|\Z)'
        def patch(match):
            body, count = re.subn(r'(?m)^(' + re.escape(key) + r'\s*=\s*)(?:true|false)', r'\g<1>true', match[2])
            if count != 1:
                raise ValueError('目标记忆开关缺失或重复：' + key)
            return match[1] + body
        text, count = re.subn(pattern, patch, text, flags=re.M | re.S)
        if count != 1:
            raise ValueError('目标记忆配置段缺失或重复：' + section)
    parsed = tomllib.loads(text)
    assert parsed['a_memorix']['plugin']['enabled']
    assert parsed['a_memorix']['integration']['feedback_correction_enabled']
    return text


if __name__ == '__main__':
    paths = [Path('/home/qqbot/QQbot/MaiBot/config/bot_config.toml'),
             Path('/mnt/d/code/web/QQbot/MaiBot/config/bot_config.toml')]
    for path in paths:
        if not path.exists():
            continue
        before = path.read_text(encoding='utf-8')
        after = enable(before)
        if before != after:
            stamp = datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')
            shutil.copy2(path, path.with_name(path.name + '.before-native-memory-' + stamp))
            path.write_text(after, encoding='utf-8')
        print('原生长期记忆和反馈纠错已启用：' + str(path))

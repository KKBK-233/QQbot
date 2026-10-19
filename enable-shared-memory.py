"""开启所有私聊和群聊的原生长期记忆共享及自然召回。"""
from datetime import datetime, timezone
from pathlib import Path
import copy
import shutil
import tomllib

import tomlkit


def main():
    """备份后更新共享范围和召回开关，回读确认其他配置不变。"""
    path = Path('/home/qqbot/QQbot/MaiBot/config/bot_config.toml')
    before = path.read_text(encoding='utf-8')
    original = tomllib.loads(before)
    config = tomlkit.parse(before)
    config['a_memorix']['global_memory_sharing_enabled'] = True
    config['a_memorix']['shared_memory_groups'] = []
    # 全局检索与自然召回使用独立开关，双向开放后才能自动引用其他会话记忆。
    recall_keys = ('heuristic_memory_recall_enabled', 'heuristic_memory_cross_chat_enabled',
                   'heuristic_memory_group_to_private_enabled', 'heuristic_memory_private_to_group_enabled')
    for key in recall_keys:
        config['a_memorix']['integration'][key] = True
    after = tomlkit.dumps(config)
    parsed = tomllib.loads(after)
    expected = copy.deepcopy(original)
    expected['a_memorix']['global_memory_sharing_enabled'] = True
    expected['a_memorix']['shared_memory_groups'] = []
    for key in recall_keys:
        expected['a_memorix']['integration'][key] = True
    assert parsed == expected, '出现共享范围之外的配置改动'
    if after != before:
        stamp = datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')
        shutil.copy2(path, path.with_name(path.name + '.before-shared-memory-' + stamp))
        path.write_text(after, encoding='utf-8')
    assert tomllib.loads(path.read_text(encoding='utf-8')) == expected
    print('全部私聊和群聊原生长期记忆共享已开启，双向自然召回已开启。')
    print('保留现有召回节流参数和历史记忆；共享摘要与知识，不逐条同步实时聊天。')


if __name__ == '__main__':
    main()

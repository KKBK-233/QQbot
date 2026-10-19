"""备份后适当缩短自动记忆周期，保留人设、模型与全共享范围。"""
from datetime import datetime, timezone
from pathlib import Path
import shutil
import tomlkit


def main():
    """减少无记忆窗口，摘要整理使用中立模式而不代入机器人性格。"""
    path = Path('/home/qqbot/QQbot/MaiBot/config/bot_config.toml')
    before = path.read_text(encoding='utf-8')
    data = tomlkit.parse(before)
    integration = data['a_memorix']['integration']
    integration['heuristic_memory_recall_min_new_messages'] = 8
    integration['heuristic_memory_recall_min_interval_seconds'] = 60
    integration['chat_summary_writeback_message_threshold'] = 18
    # 摘要专用提示词已改为中立，不另加原生未识别的配置键。
    after = tomlkit.dumps(data)
    if before != after:
        shutil.copy2(path, path.with_name(path.name + '.before-memory-v2-' + datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')))
        path.write_text(after, encoding='utf-8')
    print('自动召回8条/60秒，摘要18条；人设、模型与共享范围保持原配置')


if __name__ == '__main__':
    main()

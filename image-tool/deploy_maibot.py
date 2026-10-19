"""在 WSL 部署独立生图插件，保留运行配置，部署后由操作者重载机器人。"""

from pathlib import Path
import hashlib
import json
import shutil

source = Path('/mnt/d/code/web/QQbot/image-tool/maibot-plugin')
target = Path('/home/qqbot/QQbot/MaiBot/plugins/qqbot-image')
target.mkdir(exist_ok=True)
for name in ('_manifest.json', 'plugin.py', 'client.py', 'message_media.py', 'qq_media.py', 'media_facts.py', 'character_rules.py', 'ds_character.json', 'ai_characters.json', 'character_memory.py', 'reply_facts.py', 'web_references.py', 'reference_worker.py', 'reference_search_worker.py'):
    shutil.copy2(source / name, target / name)
    assert (source / name).read_bytes() == (target / name).read_bytes()
# 公共参考来自管理员确认的版本素材；逐项校验后放入受限生图收件目录。
inbox = Path('/mnt/d/code/web/QQbot/data/image-tool/inbox')
inbox.mkdir(parents=True, exist_ok=True)
cards = json.loads((source / 'ai_characters.json').read_text(encoding='utf-8'))
for card in cards.values():
    for profile in card['profiles'].values():
        reference = profile['reference']
        original = source.parent / 'character-assets' / reference['file']
        assert hashlib.sha256(original.read_bytes()).hexdigest() == reference['sha256']
        # Windows挂载目录只复制内容，不尝试写入Linux文件时间和权限。
        shutil.copyfile(original, inbox / reference['file'])
        assert original.read_bytes() == (inbox / reference['file']).read_bytes()
print('生图插件文件已部署并逐项核对')

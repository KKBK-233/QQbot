"""离线核对已注册的参考原图及来源摘要，不调用生图或QQ。"""

import hashlib
import json
from pathlib import Path

from PIL import Image


def verify():
    root = Path(__file__).resolve().parent
    cards = json.loads((root / 'maibot-plugin/ai_characters.json').read_text(encoding='utf-8'))
    checked = []
    for role, card in cards.items():
        for form, profile in card['profiles'].items():
            reference = profile['reference']
            path = root / 'character-assets' / reference['file']
            assert hashlib.sha256(path.read_bytes()).hexdigest() == reference['sha256']
            with Image.open(path) as image:
                image.verify()
            with Image.open(path) as image:
                assert image.width > 300 and image.height > 300
                checked.append({'character': role, 'form': form, 'format': image.format, 'size': image.size})
    return checked


if __name__ == '__main__':
    print(json.dumps(verify(), ensure_ascii=False))

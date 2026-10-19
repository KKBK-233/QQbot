"""无网络沙箱中验证参考图格式、完整性和尺寸，原始图片不重编码。"""
import base64
import io
import json
import sys
import warnings

sys.path.insert(0, '/deps')
from PIL import Image

Image.MAX_IMAGE_PIXELS = 20_000_000
warnings.simplefilter('error', Image.DecompressionBombWarning)

if __name__ == '__main__':
    try:
        request = json.loads(sys.stdin.buffer.read(8 * 1024 * 1024))
        raw = base64.b64decode(request['image'], validate=True)
        if not 12 <= len(raw) <= 5 * 1024 * 1024:
            raise ValueError('图片大小超限')
        with Image.open(io.BytesIO(raw)) as image:
            if image.format not in ('PNG', 'JPEG', 'WEBP') or getattr(image, 'is_animated', False):
                raise ValueError('不支持该图片格式')
            width, height = image.size
            if min(width, height) < 128 or width * height > 20_000_000:
                raise ValueError('图片太小或像素超限')
            image.verify()
        print(json.dumps({'success': True, 'width': width, 'height': height}))
    except Exception:
        print(json.dumps({'success': False}))

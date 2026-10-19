"""隔离目录内验证一次直请求生图，不重放群任务、不发送 QQ。"""
import argparse
import hashlib
import json
from pathlib import Path
import time

from bridge import Bridge


def main():
    """默认只核验配置；显式 live 才消耗一次现有 Codex 生图额度。"""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--live', action='store_true')
    args = parser.parse_args()
    folder = Path(__file__).resolve().parent
    config = json.loads((folder / 'config.local.json').read_text(encoding='utf-8'))
    source = Path(config['image_use'])
    assert hashlib.sha256(source.read_bytes()).hexdigest() == config['image_use_sha256']
    if not args.live:
        print('源码校验通过；未调用模型或生图。')
        return
    config['data_dir'] = str(folder.parent / 'data/image-tool-direct-smoke')
    config['codex_transport'] = 'direct'
    bridge = Bridge(config)
    rid = 'direct-' + str(int(time.time()))
    try:
        bridge.call({'action': 'generate', 'request_id': rid,
                     'prompt': '生成一张纯白背景中央只有一个蓝色圆形的图片，不含人物、文字和其他元素。'})
        bridge.pool.shutdown(wait=True)
        job = bridge.read(rid)
        trace = job.get('backend_trace', {})
        result = {'request_id': rid, 'state': job['state'], 'transport': job.get('backend_transport'),
                  'driver': trace.get('returned_driver_model'), 'image_model': trace.get('effective_image', {}).get('model'),
                  'attempts': trace.get('attempts'), 'seconds': job['elapsed_seconds'], 'qq_messages_sent': 0}
        if job['state'] != 'succeeded':
            result['error_code'] = job.get('error_code')
            print(json.dumps(result, ensure_ascii=False))
            raise SystemExit(1)
        from PIL import Image
        with Image.open(job['image_path']) as image:
            result['dimensions'] = image.size
            image.verify()
        result['image_path'] = job['image_path']
        print(json.dumps(result, ensure_ascii=False))
    finally:
        bridge.pool.shutdown(wait=True)


if __name__ == '__main__':
    main()

"""在隔离目录验证原生生图和父图编辑，不重放生产任务或发送 QQ。"""
import argparse
import hashlib
import json
from pathlib import Path
import time

from bridge import Bridge


def main():
    """显式 live 才调用一次生成和一次编辑，并保存服务端报告与图片解码证据。"""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--live', action='store_true')
    parser.add_argument('--resume', type=Path, help='仅继续本轮隔离目录尚未提交的编辑，不重放已有失败任务')
    args = parser.parse_args()
    folder = Path(__file__).resolve().parent
    config = json.loads((folder / 'config.local.json').read_text(encoding='utf-8'))
    source = Path(config['image_use'])
    assert hashlib.sha256(source.read_bytes()).hexdigest() == config['image_use_sha256']
    config.update(codex_transport='native', codex_image_model='gpt-image-2.5-sunburst', codex_image_quality='high')
    if not args.live:
        print('源码校验通过；未生成图片或发送 QQ。')
        return
    smoke_root = folder.parent / 'data/image-tool-native-smoke'
    root = args.resume.resolve() if args.resume else smoke_root / str(time.time_ns())
    if root.parent.resolve() != smoke_root.resolve():
        parser.error('只能继续既有的原生隔离验证目录')
    config['data_dir'] = str(root)
    bridge = Bridge(config)
    results = []
    try:
        # 两个任务严格先后，编辑只引用本轮已成功生成的父图。
        for rid, arguments in (
            ('generate', {'action': 'generate', 'prompt': '生成一张纯白背景中央只有一个蓝色圆形的图片，不含人物、文字和其他元素。'}),
            ('edit', {'action': 'edit', 'parent_id': 'generate', 'prompt': '只把蓝色圆形改成红色，保持圆形的大小、位置、白色背景和构图不变。'}),
        ):
            if bridge.record(rid).exists():
                if bridge.read(rid)['state'] != 'succeeded':
                    raise RuntimeError('已有任务未成功，禁止重放')
            else:
                bridge.call(dict(arguments, request_id=rid))
            while bridge.read(rid)['state'] in ('queued', 'running'):
                time.sleep(0.5)
            job = bridge.read(rid)
            trace = job.get('backend_trace', {})
            result = {'request_id': rid, 'state': job['state'], 'transport': job.get('backend_transport'),
                      'requested_image': trace.get('requested_image'), 'reported_image': trace.get('rendered_image'),
                      'reported_model': trace.get('effective_image', {}).get('model'),
                      'route': trace.get('native_route'), 'attempts': trace.get('attempts'),
                      'http_status': trace.get('http_status'), 'seconds': job['elapsed_seconds'],
                      'qq_messages_sent': 0}
            if job['state'] == 'succeeded':
                from PIL import Image
                with Image.open(job['image_path']) as decoded:
                    result['dimensions'] = list(decoded.size)
                    decoded.verify()
                result['image_path'] = job['image_path']
                result['sha256'] = hashlib.sha256(Path(job['image_path']).read_bytes()).hexdigest()
            else:
                result['error_code'] = job.get('error_code')
            results.append(result)
            evidence = root / 'verification'
            evidence.mkdir(exist_ok=True)
            (evidence / 'results.json').write_text(json.dumps(results, ensure_ascii=False, indent=2), encoding='utf-8')
            print(json.dumps(result, ensure_ascii=False), flush=True)
            if job['state'] != 'succeeded':
                raise SystemExit(1)
    finally:
        bridge.pool.shutdown(wait=True)


if __name__ == '__main__':
    main()

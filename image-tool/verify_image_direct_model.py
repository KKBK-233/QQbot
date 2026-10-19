"""隔离核对图片型号作为 Codex 顶层模型的行为，不改生产、不回退、不发 QQ。"""
import argparse
import base64
import hashlib
import json
from pathlib import Path
import re
import sys
import time
import urllib.error

from backend_trace import BackendTrace, diagnostic_text
from bridge import CODEX_IMAGE_PROXY, image_type
from codex_direct import installed_version, load_runtime


def payload_for(model):
    """请求图片模型本身输出简单图，去掉文本驱动、图片工具和思考参数。"""
    return {'model': model, 'stream': True, 'store': False,
            'instructions': 'You are an image generation assistant.',
            'input': [{'role': 'user', 'type': 'message', 'content': [
                {'type': 'input_text', 'text': '生成一张白底蓝色圆形图片。没有文字，没有人物。'}]}],
            'tools': [], 'tool_choice': 'none', 'parallel_tool_calls': False}


def main():
    """单次直调并记录脱敏元数据，网络或模型拒绝后不重试。"""
    sys.stdout.reconfigure(encoding='utf-8')
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--model', default='gpt-image-2-codex')
    parser.add_argument('--client-version', help='只对照客户端版本请求头，不安装或升级 CLI')
    parser.add_argument('--live', action='store_true', help='实际请求一次；默认只输出请求结构')
    args = parser.parse_args()
    assert re.fullmatch(r'gpt-image-[A-Za-z0-9_.-]{1,80}', args.model), '图片型号格式无效'
    assert not args.client_version or re.fullmatch(r'\d+\.\d+\.\d+', args.client_version), '版本格式无效'
    payload = payload_for(args.model)
    version = args.client_version or installed_version()
    result = {'requested_model': args.model, 'client_version': version,
              'declared_tools': [], 'model_fallback': False, 'qq_messages_sent': 0}
    if not args.live:
        print(json.dumps(dict(result, payload=payload), ensure_ascii=False, indent=2))
        return
    config = json.loads((Path(__file__).parent / 'config.local.json').read_text())
    source = Path(config['image_use'])
    assert hashlib.sha256(source.read_bytes()).hexdigest() == config['image_use_sha256'], '协议源码校验失败'
    runtime = load_runtime(source, CODEX_IMAGE_PROXY)
    access, account, _ = runtime['_extract_access_token'](runtime['_load_auth']())
    assert access, '缺少现有 Codex 授权'
    headers = runtime['_build_headers'](access, account, version)
    root = Path(__file__).resolve().parents[1] / 'data/image-direct-model-probe'
    root.mkdir(parents=True, exist_ok=True)
    record = root / (str(time.time_ns()) + '.json')
    trace = BackendTrace()
    trace.begin(payload)
    began = time.monotonic()
    images = []

    def collect_image(item):
        """只保存返回图片字节；不保存推理、请求头或整个流事件。"""
        if not isinstance(item, dict) or item.get('type') != 'image_generation_call':
            return
        encoded = item.get('result')
        if isinstance(encoded, str) and encoded and len(encoded) < 28_000_000:
            data = base64.b64decode(encoded, validate=True)
            mime = image_type(data)
            digest = hashlib.sha256(data).hexdigest()
            if digest not in images:
                suffix = {'image/png': '.png', 'image/jpeg': '.jpg', 'image/webp': '.webp'}[mime]
                output = record.with_suffix(suffix)
                output.write_bytes(data)
                result['image_path'] = str(output)
                images.append(digest)

    try:
        # 直接使用 SSE 协议层，跳过 run_codex 的模型选择和驱动固定逻辑。
        for event in runtime['_stream'](runtime['CODEX_BACKEND'], headers, payload, began + 180, 120):
            trace.event(event)
            collect_image(event.get('item'))
            if event.get('type') == 'response.completed':
                for item in (event.get('response') or {}).get('output', []):
                    collect_image(item)
        result['trace'] = trace.data
        result['errors'] = trace.diagnostic.data['errors']
    except Exception as error:
        trace.failure(error)
        result['trace'] = trace.data
        if isinstance(error, urllib.error.HTTPError):
            raw = error.read(8192).decode('utf-8', errors='replace')
            try:
                detail = json.loads(raw)
                detail = detail.get('error', detail.get('detail', detail)) if isinstance(detail, dict) else detail
                result['server_error'] = ({key: diagnostic_text(detail.get(key)) for key in ('code', 'type', 'message')}
                                          if isinstance(detail, dict) else diagnostic_text(str(detail)))
            except ValueError:
                result['server_error'] = diagnostic_text(raw)
        else:
            result['error_type'] = type(error).__name__
    result['seconds'] = round(time.monotonic() - began, 2)
    result['image_count'] = len(images)
    record.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding='utf-8')
    print(json.dumps(result, ensure_ascii=False, indent=2))
    print('证据：' + str(record))


if __name__ == '__main__':
    main()

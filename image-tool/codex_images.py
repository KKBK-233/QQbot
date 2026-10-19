"""使用现有 Codex OAuth 直调原生图片接口，生成与参考编辑共用限流和诊断。"""
import base64
import json
from pathlib import Path
import threading
import time
from types import SimpleNamespace
import urllib.error
import urllib.request
import uuid

from backend_trace import BackendTrace
from codex_direct import installed_version, load_runtime

MAX_IMAGE = 20 * 1024 * 1024
MAX_RESPONSE = 32 * 1024 * 1024
AUTH_REFRESH_LOCK = threading.Lock()
BASE_URL = 'https://chatgpt.com/backend-api/codex/'


def image_mime(data):
    """校验引用和输出的格式及体积，不把网址、HTML 或空结果当成图片。"""
    if not 12 <= len(data) <= MAX_IMAGE:
        raise ValueError('invalid_image: 图片为空、过小或超过20MB')
    if data.startswith(b'\x89PNG\r\n\x1a\n'):
        return 'image/png'
    if data.startswith(b'\xff\xd8\xff'):
        return 'image/jpeg'
    if data[:4] == b'RIFF' and data[8:12] == b'WEBP':
        return 'image/webp'
    raise ValueError('invalid_image_format: 只接受 PNG、JPEG、WEBP')


def generate(source, prompt, refs, model, background, proxy, concurrency, diagnostic_path, *, quality='high'):
    """只提交图片请求；401 可刷新一次，限流、内容拒绝和不确定网络失败均不重放。"""
    trace = BackendTrace()
    image = None
    error_data = b''
    try:
        payload = {'model': model, 'prompt': prompt, 'n': 1, 'quality': quality, 'size': 'auto',
                   'background': background or 'opaque'}
        route = 'images/edits' if refs else 'images/generations'
        if refs:
            payload['images'] = []
            for path in refs:
                path = Path(path)
                if path.stat().st_size > MAX_IMAGE:
                    raise ValueError('invalid_image: 参考图片超过20MB')
                data = path.read_bytes()
                mime = image_mime(data)
                payload['images'].append({'image_url': f'data:{mime};base64,' + base64.b64encode(data).decode()})
        body = json.dumps(payload, ensure_ascii=False).encode('utf-8')
        runtime = load_runtime(source, proxy)
        began = time.monotonic()
        # 复用已校验源码的账号槽与 OAuth；不调用其 Responses 生图或文字驱动。
        with runtime['_concurrency_slot']('codex', concurrency, False, began):
            auth = runtime['_load_auth']()
            access, account, refresh = runtime['_extract_access_token'](auth)
            if not access:
                raise ValueError('authentication_error: 缺少 Codex OAuth')
            turn_id = str(uuid.uuid4())
            for attempt in range(2):
                headers = runtime['_build_headers'](access, account, installed_version())
                headers.update({'Accept': 'application/json', 'originator': 'qqbot-image-bridge',
                                'User-Agent': 'qqbot-image-bridge/1.4.0 (Windows)',
                                'x-codex-image-turn-id': turn_id})
                request = urllib.request.Request(BASE_URL + route, data=body, headers=headers, method='POST')
                trace.begin_images(payload, route, len(body))
                try:
                    # 原生接口在返回响应头前也可能等待整张图，不能沿用 SSE 的短等待。
                    with runtime['urllib'].request.urlopen(request, timeout=600) as response:
                        trace.data['http_status'] = response.status
                        trace.data['accepted'] = True
                        raw = response.read(MAX_RESPONSE + 1)
                    if len(raw) > MAX_RESPONSE:
                        raise ValueError('no image returned: 响应超过32MB')
                    result = json.loads(raw)
                    if not isinstance(result, dict):
                        raise ValueError('no image returned: 响应格式错误')
                    trace.images_response(result)
                    entries = result.get('data')
                    if not isinstance(entries, list) or len(entries) != 1 or not isinstance(entries[0], dict):
                        raise ValueError('no image returned: 未返回单张图片')
                    encoded = entries[0].get('b64_json')
                    if not isinstance(encoded, str) or len(encoded) > ((MAX_IMAGE + 2) // 3) * 4:
                        raise ValueError('no image returned: 缺少图片字节或图片过大')
                    try:
                        image = base64.b64decode(encoded, validate=True)
                    except ValueError:
                        raise ValueError('no image returned: 图片 base64 无效') from None
                    image_mime(image)
                    trace.data.update(response_status='completed', image_result_present=True,
                                      image_call_statuses=['completed'])
                    trace.diagnostic.data['response_status'] = 'completed'
                    break
                except urllib.error.HTTPError as error:
                    trace.data['http_status'] = error.code
                    try:
                        detail = json.loads(error.read(8192))
                    except (ValueError, OSError):
                        detail = {}
                    trace.images_error(detail)
                    if error.code != 401 or attempt or not refresh:
                        code = trace.data.get('error_code', '')
                        raise RuntimeError(f'HTTP {error.code} {code}') from None
                    # 并发任务先检查另一任务是否已刷新，避免重复使用旋转的刷新令牌。
                    with AUTH_REFRESH_LOCK:
                        current = runtime['_load_auth']()
                        new_access, new_account, new_refresh = runtime['_extract_access_token'](current)
                        if new_access and new_access != access:
                            access, account, refresh = new_access, new_account, new_refresh
                        else:
                            updated = runtime['_refresh_access_token'](refresh)
                            runtime['_persist_refreshed_auth'](auth, updated)
                            access, account, refresh = runtime['_extract_access_token'](auth)
                            if not access:
                                raise ValueError('authentication_error: 刷新未取得令牌')
    except (Exception, SystemExit) as error:
        image = None
        trace.failure(error)
        trace.data.update(response_status='failed', image_result_present=False)
        trace.diagnostic.data['response_status'] = 'failed'
        # 返回分类文本，不在群里输出上游正文；服务端说明仅保存在脱敏本机诊断。
        error_data = (trace.data.get('error_code', '') + ' ' + type(error).__name__ + ': ' + str(error)).encode('utf-8', errors='replace')[:8192]
    finally:
        trace.data['diagnostic_written'] = trace.diagnostic.save(diagnostic_path)
    return SimpleNamespace(image=image, trace=trace.data, error_data=error_data)

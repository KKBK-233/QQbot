"""真实Codex响应核对驱动和默认图片配置；禁止调用生图工具，不发送QQ。"""
import hashlib
import argparse
import json
import os
from pathlib import Path
import runpy
import re
import sys
import time
import urllib.request

PHASE = 'initialize'

from backend_trace import BackendTrace, read_trace, safe_model
from bridge import backend_environment
from image_use_runner import pin_codex_driver


def main():
    """只提交一轮短文本验证，读取服务器公布的生效工具配置，不猜底层型号。"""
    sys.stdout.reconfigure(encoding='utf-8')
    global PHASE
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--driver', default='gpt-6.1-sol', help='本次只读核对的驱动型号，不改变生产配置')
    parser.add_argument('--image-model', default=None, help='可选对照图片型号，只核对生效配置，不生图')
    parser.add_argument('--quality', choices=('auto', 'low', 'medium', 'high', 'xhigh', 'max'), default=None)
    parser.add_argument('--client-version', default=None, help='只读对比新版客户端协议，不安装或修改本机CLI')
    args = parser.parse_args()
    driver = args.driver
    assert safe_model(driver), '驱动型号标识无效'
    assert args.image_model is None or safe_model(args.image_model), '图片型号标识无效'
    env = backend_environment('codex')
    os.environ.clear()
    os.environ.update(env)
    config = json.loads((Path(__file__).parent / 'config.local.json').read_text())
    source = Path(config['image_use'])
    assert hashlib.sha256(source.read_bytes()).hexdigest() == config['image_use_sha256'], '上游源码校验失败'
    runtime = runpy.run_path(str(source), run_name='image_model_readonly_probe')
    pin_codex_driver(runtime, driver)
    auth = runtime['_load_auth']()
    access, account_id, _refresh = runtime['_extract_access_token'](auth)
    assert access, '本机Codex登录凭据不存在'
    version = json.loads((Path(os.environ['APPDATA']) / 'npm/node_modules/@openai/codex/package.json').read_text())['version']
    if args.client_version:
        assert re.fullmatch(r'\d+\.\d+\.\d+', args.client_version), '客户端版本无效'
        version = args.client_version
    headers = runtime['_build_headers'](access, account_id, version)
    models_request = urllib.request.Request('https://chatgpt.com/backend-api/codex/models?client_version=' + version, headers=headers)
    PHASE = 'account_models'
    with urllib.request.urlopen(models_request, timeout=20) as response:
        models = json.loads(response.read(2 * 1024 * 1024))
    available = any(value.get('slug') == driver or value.get('id') == driver
                    for value in models.get('models', []) if isinstance(value, dict))
    payload = runtime['_build_payload']('请只回复OK。不要调用任何工具，不要生成图片。', 'auto', 'png', driver,
                                       image_model=args.image_model, quality=args.quality, background='opaque')
    payload['tool_choice'] = 'none'
    trace = BackendTrace()
    trace.begin(payload)
    PHASE = 'text_response'
    began = time.monotonic()
    for event in runtime['_stream']('https://chatgpt.com/backend-api/codex/responses', headers, payload, began + 90, 30):
        trace.event(event)
    result = read_trace(trace.line().encode())
    assert result.get('accepted') and result['events'].get('response.completed'), '文本请求未完成'
    assert not any(name.startswith('response.image_generation_call.') for name in result['events']), '服务器意外调用生图工具'
    assert result.get('returned_driver_model') == driver, '服务器驱动型号与请求不符'
    assert result.get('effective_image', {}).get('model'), '服务器没有返回图片工具型号'
    output = {'account_lists_requested_driver': available, 'seconds': round(time.monotonic()-began, 2),
              'client_version': version, 'trace': result, 'image_generation_calls': 0, 'qq_messages_sent': 0}
    folder = Path(__file__).resolve().parents[1] / 'data/image-model-inspection'
    folder.mkdir(parents=True, exist_ok=True)
    (folder / ('server-explicit.json' if args.image_model else 'server-default.json')).write_text(json.dumps(output, ensure_ascii=False, indent=2), encoding='utf-8')
    print(json.dumps(output, ensure_ascii=False, indent=2))


if __name__ == '__main__':
    try:
        main()
    except Exception as error:
        # 不打印上游错误原文或HTTP正文，避免包含凭据、模型回复及工具输入。
        markers = []
        if hasattr(error, 'read'):
            message = error.read(8192).decode('utf-8', errors='replace').lower()
            markers = [word for word in ('tool_choice', 'none', 'instructions', 'model', 'not supported', 'required', 'tools') if word in message]
        print(json.dumps({'success': False, 'phase': PHASE, 'error_type': type(error).__name__,
                          'http_status': getattr(error, 'status', None), 'error_markers': markers}, ensure_ascii=False))
        raise SystemExit(1)

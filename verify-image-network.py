"""按生图子进程的实际网络配置只读验权，不提交图片生成请求。"""
import argparse
import json
import os
from pathlib import Path
import sys
import time
import uuid
import urllib.error
import urllib.request

sys.path.insert(0, str(Path(__file__).parent / 'image-tool'))
from bridge import backend_environment


def main():
    """账号信息留在内存，仅输出状态与时间；大请求使用无效模型且不含生成工具。"""
    sys.stdout.reconfigure(encoding='utf-8')
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--large-request', action='store_true')
    parser.add_argument('--validation-bytes', type=int, default=220000)
    args = parser.parse_args()
    env = backend_environment('codex')
    # 与bridge给子进程的环境一致，用urllib默认入口而非另建手动代理。
    os.environ.clear()
    os.environ.update(env)
    auth = json.loads((Path.home() / '.codex/auth.json').read_text(encoding='utf-8'))
    tokens = auth.get('tokens') or {}
    assert tokens.get('access_token'), '本机OAuth不存在'
    version = json.loads((Path.home() / 'AppData/Roaming/npm/node_modules/@openai/codex/package.json').read_text())['version']
    headers = {'Authorization': 'Bearer ' + tokens['access_token'], 'User-Agent': 'codex_cli_rs/' + version}
    if tokens.get('account_id'):
        headers['ChatGPT-Account-ID'] = tokens['account_id']
    request = urllib.request.Request('https://chatgpt.com/backend-api/codex/models?client_version=' + version, headers=headers)
    began = time.monotonic()
    with urllib.request.urlopen(request, timeout=15) as response:
        data = json.loads(response.read(2 * 1024 * 1024))
        assert response.status == 200 and isinstance(data.get('models'), list)
    print(json.dumps({'route': env['HTTPS_PROXY'], 'oauth_http_status': 200,
                      'seconds': round(time.monotonic()-began,2)}))
    if args.large_request:
        # 匿名大请求可能在上传完之前被认证层拒绝；使用无效模型验证完整上传，绝不生图。
        assert 0 <= args.validation_bytes <= 2 * 1024 * 1024
        body = json.dumps({'model':'__qqbot_network_probe_invalid__', 'tools':[], 'input':[],
                           'store':False, 'stream':True, 'instructions':'x' * args.validation_bytes}).encode()
        session = str(uuid.uuid4())
        req = urllib.request.Request('https://chatgpt.com/backend-api/codex/responses', data=body,
                                     headers={**headers, 'Content-Type':'application/json', 'Accept':'text/event-stream',
                                              'originator':'codex_cli_rs', 'version':version,
                                              'session_id':session, 'x-client-request-id':session}, method='POST')
        began = time.monotonic()
        try:
            with urllib.request.urlopen(req, timeout=20) as response:
                status = response.status
        except urllib.error.HTTPError as error:
            status = error.code
        except urllib.error.URLError as error:
            print(json.dumps({'upload_error_type':type(error.reason).__name__,
                              'seconds':round(time.monotonic()-began,2), 'generation_requests':0}))
            raise SystemExit(1) from None
        assert status in (400,401,403,422), '无效模型探测返回意外状态'
        print(json.dumps({'validation_upload_bytes':len(body), 'http_status':status,
                          'seconds':round(time.monotonic()-began,2), 'generation_requests':0}))


if __name__ == '__main__':
    main()

"""本地缺图时按明确消息编号向本机 QQ 补取，先校验所属群。"""
import asyncio
import base64
import json
import re
from pathlib import Path
from urllib.parse import urlsplit
from urllib.request import Request, build_opener, ProxyHandler, HTTPRedirectHandler


MAX_IMAGE_BYTES = 20 * 1024 * 1024
QQ_IMAGE_HOSTS = {'multimedia.nt.qq.com.cn', 'gchat.qpic.cn', 'c2cpicdw.qpic.cn'}
TOKEN_FILE = Path('/home/qqbot/QQbot/run/onebot.token')


class NoRedirect(HTTPRedirectHandler):
    """补取仅访问已核验地址，不跟随跳转到其他服务。"""
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def read_bounded(response, limit):
    """限制实际读取长度，不依赖远端声明的图片大小。"""
    data = response.read(limit + 1)
    if len(data) > limit:
        raise ValueError('补取数据超出大小限制')
    return data


def image_bytes(data):
    """只接收常见图片文件，错误页不会作为图片交给模型。"""
    return (data.startswith(b'\x89PNG\r\n\x1a\n') or data.startswith(b'\xff\xd8\xff')
            or data.startswith((b'GIF87a', b'GIF89a'))
            or (data.startswith(b'RIFF') and data[8:12] == b'WEBP'))


class QQMediaClient:
    """固定本机 OneBot 接口；凭据和带签名的图片地址不进入模型或日志。"""
    def __init__(self):
        self.opener = build_opener(ProxyHandler({}), NoRedirect())

    def call(self, action, args):
        token = TOKEN_FILE.read_text(encoding='utf-8').strip()
        req = Request('http://127.0.0.1:3000/' + action,
                      data=json.dumps(args).encode(),
                      headers={'Authorization': 'Bearer ' + token, 'Content-Type': 'application/json'})
        with self.opener.open(req, timeout=0.5) as response:
            result = json.loads(read_bounded(response, 1024 * 1024))
        if result.get('status') != 'ok' or result.get('retcode') != 0:
            raise ValueError('QQ 补取接口未成功')
        return result['data']

    def fetch_image(self, file):
        info = self.call('get_image', {'file': file})
        url = info.get('url') or info.get('file') or ''
        parsed = urlsplit(url)
        if (parsed.scheme != 'https' or parsed.hostname not in QQ_IMAGE_HOSTS
                or parsed.username or parsed.password or parsed.port not in (None, 443)):
            raise ValueError('QQ 未返回受信图片地址')
        # 仅使用平台返回的 QQ 原图地址，图片请求不携带 OneBot 凭据。
        with self.opener.open(Request(url), timeout=0.8) as response:
            data = read_bounded(response, MAX_IMAGE_BYTES)
        if not image_bytes(data):
            raise ValueError('QQ 补取结果不是图片')
        return base64.b64encode(data).decode('ascii')

    def get_message(self, session, group, mid, max_images):
        raw = self.call('get_msg', {'message_id': int(mid)})
        # 必须先确认消息编号和群归属，之后才访问任何图片。
        if (str(raw.get('message_id')) != mid or raw.get('message_type') != 'group'
                or str(raw.get('group_id')) != group):
            return None
        parts = []
        count = 0
        for part in raw.get('message', [])[:200]:
            if not isinstance(part, dict) or not isinstance(part.get('data'), dict):
                continue
            data = part['data']
            if part.get('type') == 'reply' and re.fullmatch(r'-?\d{1,20}', str(data.get('id', ''))):
                parts.append({'type': 'reply', 'data': {'target_message_id': str(data['id'])}})
            elif part.get('type') == 'image' and count < max_images and isinstance(data.get('file'), str):
                parts.append({'type': 'image', 'binary_data_base64': self.fetch_image(data['file'])})
                count += 1
        return {'message_id': mid, 'session_id': session, 'raw_message': parts}


async def recover_message(ctx, session, mid, max_images=2):
    """会话来自宿主真实群映射；补取失败保留原来的缺图行为。"""
    chat = getattr(ctx, 'chat', None)
    if not chat or not re.fullmatch(r'-?\d{1,20}', mid):
        return None
    try:
        streams = await chat.get_group_streams(platform='qq')
        if isinstance(streams, dict):
            streams = streams.get('streams', [])
        groups = {str(s.get('group_id')) for s in streams or [] if isinstance(s, dict)
                  and s.get('session_id') == session and s.get('platform') == 'qq'
                  and s.get('group_id')}
        if len(groups) != 1:
            return None
        return await asyncio.to_thread(QQMediaClient().get_message, session, groups.pop(), mid,
                                       max(1, min(int(max_images), 2)))
    except (OSError, ValueError, TypeError, KeyError, RuntimeError):
        return None

"""在独立 Chrome 临时会话人工验证公众号，并把同会话正文交付给网页工具。"""
from pathlib import Path
from urllib.parse import urljoin, urlsplit

import argparse
import asyncio
import json
import os
import re
import sys
import time

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT / 'MaiBot/plugins/qqbot-web-search'))
from manual_article import article_url, cache_name

CAPTCHA_DOMAINS = ('captcha.qq.com', 'captcha.qcloud.com')
READ_HOSTS = {'mp.weixin.qq.com', 'res.wx.qq.com', 'captcha.gtimg.com', 'tcaptcha.gtimg.com',
              'mmbiz.qpic.cn', 'mmbiz.qlogo.cn'}


def allowed_request(url: str, method: str, resource_type: str, *, main_frame=False) -> bool:
    """仅开放微信正文及腾讯验证码资源；POST 只用于此次人工挑战，不开放其他站点。"""
    parsed = urlsplit(url)
    if (parsed.scheme != 'https' or parsed.port not in (None, 443)
            or parsed.username is not None or parsed.password is not None):
        return False
    host = parsed.hostname
    captcha = host in {'captcha.gtimg.com', 'tcaptcha.gtimg.com'} or any(
        host == domain or host and host.endswith('.' + domain) for domain in CAPTCHA_DOMAINS)
    if main_frame and resource_type == 'document':
        return method == 'GET' and host == 'mp.weixin.qq.com' and (
            parsed.path == '/s' or parsed.path.startswith('/s/') or parsed.path == '/mp/wappoc_appmsgcaptcha')
    if resource_type not in ('document', 'script', 'stylesheet', 'image', 'font', 'xhr', 'fetch'):
        return False
    if method == 'GET':
        return host in READ_HOSTS or captcha
    return method == 'POST' and (captcha or host == 'mp.weixin.qq.com' and parsed.path == '/mp/wappoc_appmsgcaptcha')


def save_article(url: str, snapshot: dict) -> None:
    """原子交付最多10000字的公开正文；不保存浏览器 Cookie、账号或挑战令牌。"""
    target = ROOT / 'data/web-articles'
    target.mkdir(parents=True, exist_ok=True)
    data = {'url': article_url(url), 'title': snapshot['title'][:240],
            'text': snapshot['text'][:10000], 'truncated': len(snapshot['text']) > 10000,
            'created_at': time.time()}
    path = target / cache_name(url)
    temporary = path.with_suffix('.tmp')
    temporary.write_text(json.dumps(data, ensure_ascii=False), encoding='utf-8')
    os.replace(temporary, path)


def same_article(original: str, current: str) -> bool:
    """只交付当前原文章，避免手动打开其他文章后误写到原链接缓存。"""
    try:
        return article_url(original) == article_url(current)
    except ValueError:
        return False


async def main(args):
    """验证完全由人操作；程序只监听真实正文，完成后继续保留该临时窗口。"""
    from playwright.async_api import async_playwright
    url = article_url(args.url)
    async with async_playwright() as driver:
        browser = await driver.chromium.launch(channel='chrome', headless=False, chromium_sandbox=True,
            proxy={'server': args.proxy} if args.proxy else None,
            args=['--disable-extensions', '--disable-background-networking', '--disable-quic'])
        context = await browser.new_context(service_workers='block', accept_downloads=False)
        page = await context.new_page()
        # 不暴露个人浏览器配置、文件或宿主接口；第三方跳转及网络发送按固定域名限定。
        async def guard(route):
            request = route.request
            main_frame = request.frame == page.main_frame
            target = request.url
            if not allowed_request(target, request.method, request.resource_type, main_frame=main_frame):
                await route.abort()
                return
            # 自动重定向可能绕过路由拦截；每一跳先检查域名，再由程序显式读取。
            response = None
            try:
                for hop in range(4):
                    response = await route.fetch(url=target, max_redirects=0, timeout=20000)
                    if response.status not in (301, 302, 303, 307, 308):
                        await route.fulfill(response=response)
                        return
                    next_url = urljoin(target, response.headers.get('location', ''))
                    if (hop == 3 or request.method != 'GET' or not response.headers.get('location')
                            or not allowed_request(next_url, 'GET', request.resource_type, main_frame=main_frame)):
                        await route.abort()
                        return
                    if main_frame and request.resource_type == 'document':
                        # 保留临时会话 Cookie，以新的导航触发下一跳检查，不自动操作验证码。
                        destination = json.dumps(next_url).replace('<', '\\u003c')
                        await route.fulfill(status=200, content_type='text/html',
                                            body='<script>location.replace(' + destination + ')</script>')
                        return
                    await response.dispose()
                    response = None
                    target = next_url
            except Exception:
                await route.abort()
            finally:
                if response is not None:
                    await response.dispose()
        await context.route('**/*', guard)
        await context.route_web_socket('**/*', lambda websocket: websocket.close())
        page.on('dialog', lambda dialog: dialog.dismiss())
        context.on('page', lambda popup: asyncio.create_task(popup.close()) if popup != page else None)
        saved = False
        print('公众号验证窗口已打开。请自己点击“去验证”并完成题目；无需登录微信。', flush=True)
        print('验证后将从同一窗口提取正文；如果未自动跳转，请在该窗口重新打开原文章。', flush=True)
        try:
            try:
                await page.goto(url, wait_until='domcontentloaded', timeout=30000)
            except Exception as error:
                code = re.search(r'net::ERR_[A-Z_]+', str(error))
                print('初次导航未完成：' + (code.group(0) if code else type(error).__name__)
                      + '。窗口继续保留，可在该窗口手动刷新。', flush=True)
            print('状态：等待页面显示正文或人工完成验证。', flush=True)
            deadline = time.monotonic() + 1800
            while not page.is_closed() and time.monotonic() < deadline:
                # 只提取微信正文节点，不把验证页、导航文字当成文章，也不点击或解答挑战。
                if same_article(url, page.url):
                    try:
                        snapshot = await page.evaluate('''() => {
                            const article = document.querySelector('#js_content');
                            if (!article || !article.getClientRects().length) return null;
                            const text = (article.innerText || '').trim();
                            const title = (document.querySelector('#activity-name')?.innerText || document.title || '').trim();
                            return text ? {text: text.slice(0, 10001), title} : null;
                        }''')
                    except Exception:
                        snapshot = None
                    if snapshot and not saved:
                        save_article(url, snapshot)
                        saved = True
                        print('正文已交给机器人，半小时内可让它重新读取同一链接。窗口保留到你关闭，最长30分钟。', flush=True)
                await asyncio.sleep(1)
            if not saved:
                print('未取得真实文章正文，没有交付验证提示或空白页面。', flush=True)
        except Exception as error:
            # 页面异常可能包含临时令牌，只输出异常类型，不打印完整 URL 或堆栈。
            code = re.search(r'net::ERR_[A-Z_]+', str(error))
            print('公众号验证入口失败：' + (code.group(0) if code else type(error).__name__), flush=True)
        finally:
            await browser.close()


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--url', required=True, help='微信公开文章原链接')
    parser.add_argument('--proxy', default='http://127.0.0.1:2080', help='默认使用现有本机代理')
    options = parser.parse_args()
    asyncio.run(main(options))

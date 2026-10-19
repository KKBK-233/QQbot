"""仅监听本机的 ChatGPT 登录与账号管理页；不提供令牌读取接口。"""

from html import escape
from typing import Any, Dict

import asyncio
import secrets

from aiohttp import web

from auth import AccountStore, AuthError

PORT = 18161
BASE = f"http://127.0.0.1:{PORT}"
store = AccountStore()
pending: Dict[str, Dict[str, Any]] = {}
csrf = secrets.token_urlsafe(32)
last_result = "需要恢复授权时，请点击原账号下的“重新登录此账号”。"


def page(body: str, status: int = 200) -> web.Response:
    return web.Response(text='<!doctype html><html lang="zh-CN"><meta charset="utf-8">'
                        '<meta name="viewport" content="width=device-width,initial-scale=1">'
                        '<title>MaiBot · ChatGPT 登录</title><style>'
                        'body{max-width:720px;margin:64px auto;padding:24px;font:17px system-ui;background:#f6f7f9;color:#20232a}'
                        'button{padding:12px 20px;margin:8px 8px 8px 0;border:0;border-radius:8px;background:#20232a;color:white;cursor:pointer}'
                        'article{padding:20px;background:white;border-radius:12px;margin:16px 0}small{color:#63676f}'
                        '</style><h1>MaiBot · ChatGPT 账号</h1>' + body + '</html>',
                        content_type="text/html", status=status,
                        # 同源表单保留来源供请求校验，跨站授权仍不发送页面来源。
                        headers={"Cache-Control": "no-store", "Referrer-Policy": "same-origin",
                                 "Content-Security-Policy": "default-src 'none'; style-src 'unsafe-inline'; form-action 'self' https://auth.openai.com; frame-ancestors 'none'"})


@web.middleware
async def local_only(request: web.Request, handler):
    if request.host != f"127.0.0.1:{PORT}":
        raise web.HTTPForbidden(text="仅允许本机访问")
    if request.method == "POST":
        form = await request.post()
        if request.headers.get("Origin") not in (None, BASE) or not secrets.compare_digest(str(form.get("csrf", "")), csrf):
            # 服务重启会更换表单校验值；旧标签页给出可恢复入口，仍拒绝执行账号操作。
            return page('<p>此页面的操作已失效，请重新打开账号页后再操作。</p>'
                        '<p>恢复授权请点击原账号下的“重新登录此账号”。</p>'
                        '<a href="/">重新打开账号页</a>', 403)
    return await handler(request)


def button(action: str, label: str, key: str = "") -> str:
    if action == '/login':
        return f'<p><a href="/authorize?account={escape(key)}">{escape(label)}</a></p>'
    return f'<form method="post" action="{action}"><input type="hidden" name="csrf" value="{csrf}"><input type="hidden" name="account" value="{escape(key)}"><button>{escape(label)}</button></form>'


async def index(request: web.Request) -> web.Response:
    status = await asyncio.to_thread(store.public_status)
    body = f'<p>{escape(last_result)}</p><article><b>模型沿用麦麦当前配置</b><p>由 MaiBot 管理人格、记忆、消息频率和工具执行。</p>{button("/login", "Continue with ChatGPT")}</article>'
    for account in status["accounts"]:
        body += '<article>' + escape(account["email"] or account["key"]) + (' · 当前账号' if account["active"] else '')
        body += button('/login', '重新登录此账号', account['key'])
        if account['signed_in']:
            if account['active']:
                body += '<p>当前账号已启用，无需再次点击使用。</p>'
            else:
                body += button('/select', '使用此账号', account['key'])
            body += button('/logout', '退出此账号', account['key'])
        body += '</article>'
    body += '<p>“使用此账号”仅切换已保存的账号；授权过期或失效时，请选择“重新登录此账号”。</p>'
    body += '<p><small>凭据仅保存在本机独立文件。此页不会读取或修改 Codex 的登录。</small></p><a href="https://chatgpt.com/settings/usage" rel="noreferrer">查看 ChatGPT 用量与应用授权</a>'
    return page(body)


async def login(request: web.Request) -> web.Response:
    form = request.query if request.method == 'GET' else await request.post()
    attempt = await asyncio.to_thread(store.begin, BASE + '/auth/callback', str(form.get('account', '')) or None)
    # 每次登录互不覆盖，回调使用 state 定位并一次性消耗；过期项及时清除。
    import time
    for key, value in list(pending.items()):
        if time.time() - value['created'] > 600:
            pending.pop(key)
    pending[attempt['state']] = attempt
    raise web.HTTPFound(attempt['url'], headers={'Cache-Control': 'no-store', 'Referrer-Policy': 'no-referrer'})


async def callback(request: web.Request) -> web.Response:
    global last_result
    attempt = pending.pop(request.query.get('state', ''), None)
    if attempt is None:
        return page('<p>登录状态无效或已使用，请重新登录。</p><a href="/">返回</a>', 400)
    try:
        await asyncio.to_thread(store.complete, attempt, dict(request.query))
        last_result = '登录成功。账号凭据已保存并自动启用，无需再点击使用。模型沿用麦麦当前配置。'
    except AuthError as exc:
        last_result = str(exc)
    except Exception as exc:
        # 第三方异常可能带回令牌片段，只显示类型，不打印原始异常。
        last_result = '登录验证失败：' + type(exc).__name__
    return page(f'<p>{escape(last_result)}</p><a href="/">返回账号页</a>')


async def select(request: web.Request) -> web.Response:
    await asyncio.to_thread(store.select, str((await request.post()).get('account', '')))
    raise web.HTTPSeeOther('/')


async def logout(request: web.Request) -> web.Response:
    await asyncio.to_thread(store.sign_out, str((await request.post()).get('account', '')))
    raise web.HTTPSeeOther('/')


async def return_to_account_page(request: web.Request) -> web.Response:
    """直接打开或刷新操作地址时回到账户页，GET 请求不修改账号或凭据。"""
    raise web.HTTPSeeOther('/')


app = web.Application(middlewares=[local_only])
app.add_routes([web.get('/', index), web.get('/authorize', login), web.post('/login', login), web.get('/auth/callback', callback),
                web.post('/select', select), web.post('/logout', logout),
                web.get('/select', return_to_account_page), web.get('/logout', return_to_account_page),
                web.get('/login', return_to_account_page)])
if __name__ == '__main__':
    web.run_app(app, host='127.0.0.1', port=PORT, access_log=None, print=lambda _: None)

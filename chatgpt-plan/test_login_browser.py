"""用隔离 Chromium 验证真实表单来源与提交，不接触真实账号或个人浏览器。"""

import asyncio
from pathlib import Path
from unittest.mock import Mock, patch

from aiohttp import web
from aiohttp.test_utils import TestServer
from playwright.async_api import async_playwright

import login_server as server


async def main():
    browsers = Path("/home/qqbot/QQbot/run/web-browsers")
    executable = next(browsers.glob("chromium_headless_shell-*/chrome-headless-shell-linux64/chrome-headless-shell"))
    store = Mock()
    store.public_status.return_value = {
        "authenticated": True,
        "accounts": [
            {"key": "current", "email": "current@example.invalid", "active": True, "signed_in": True},
            {"key": "other", "email": "other@example.invalid", "active": False, "signed_in": True},
        ],
    }
    app = web.Application(middlewares=[server.local_only])
    for route in server.app.router.routes():
        app.router.add_route(route.method, route.resource.canonical, route.handler)
    host = TestServer(app)
    await host.start_server()
    base = str(host.make_url("/")).rstrip("/")
    try:
        with patch.object(server, "store", store), patch.object(server, "PORT", host.port), patch.object(server, "BASE", base):
            async with async_playwright() as driver:
                browser = await driver.chromium.launch(headless=True, executable_path=str(executable))
                try:
                    page = await browser.new_page()
                    response = await page.goto(base + "/")
                    assert response.status == 200
                    assert response.headers["referrer-policy"] == "same-origin"
                    assert await page.get_by_role("button", name="使用此账号", exact=True).count() == 1
                    async with page.expect_response(lambda r: r.url == base + "/select") as submitted:
                        await page.get_by_role("button", name="使用此账号", exact=True).click()
                    result = await submitted.value
                    assert result.request.headers.get("origin") == base
                    assert result.status == 303
                    await page.wait_for_url(base + "/")
                    store.select.assert_called_once_with("other")
                    store.sign_out.assert_not_called()
                    print("真实浏览器验证通过：同源表单 Origin 正确，账号选择 POST 303 并返回首页；当前账号不显示重复启用按钮。")
                finally:
                    await browser.close()
    finally:
        await host.close()


if __name__ == "__main__":
    asyncio.run(main())

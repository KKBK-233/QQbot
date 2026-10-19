"""验证操作地址刷新恢复、POST 跳转和账号操作的请求校验。"""

import unittest
from unittest.mock import Mock, patch

from aiohttp.test_utils import TestClient, TestServer
from aiohttp import web

import login_server as server


class LoginPageTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.store = Mock()
        self.store.public_status.return_value = {
            "authenticated": True,
            "accounts": [{"key": "example", "email": "test@example.invalid",
                          "active": True, "signed_in": True}],
        }
        self.patch = patch.object(server, "store", self.store)
        self.patch.start()
        # 复用生产路由，隔离服务器生命周期；不读取或修改真实账号。
        app = web.Application(middlewares=[server.local_only])
        for route in server.app.router.routes():
            app.router.add_route(route.method, route.resource.canonical, route.handler)
        self.client = TestClient(TestServer(app))
        await self.client.start_server()
        self.headers = {"Host": "127.0.0.1:18161"}

    async def asyncTearDown(self):
        await self.client.close()
        self.patch.stop()

    async def test_get_operation_urls_return_home_without_mutation(self):
        for path in ("/select", "/logout", "/login"):
            response = await self.client.get(path, headers=self.headers, allow_redirects=False)
            self.assertEqual(response.status, 303)
            self.assertEqual(response.headers["Location"], "/")
        self.store.select.assert_not_called()
        self.store.sign_out.assert_not_called()
        self.store.begin.assert_not_called()

    async def test_post_selection_returns_get_redirect(self):
        response = await self.client.post(
            "/select", data={"csrf": server.csrf, "account": "example"},
            headers={**self.headers, "Origin": server.BASE}, allow_redirects=False)
        self.assertEqual(response.status, 303)
        self.assertEqual(response.headers["Location"], "/")
        self.store.select.assert_called_once_with("example")

    async def test_invalid_csrf_and_origin_cannot_change_account(self):
        for data, origin in (({"account": "example"}, server.BASE),
                             ({"csrf": server.csrf, "account": "example"}, "https://example.invalid")):
            response = await self.client.post(
                "/select", data=data, headers={**self.headers, "Origin": origin}, allow_redirects=False)
            self.assertEqual(response.status, 403)
        self.store.select.assert_not_called()

    async def test_stale_form_shows_recovery_without_selecting_account(self):
        response = await self.client.post(
            "/select", data={"csrf": "old-page-token", "account": "example"},
            headers={**self.headers, "Origin": server.BASE}, allow_redirects=False)
        text = await response.text()
        self.assertEqual(response.status, 403)
        self.assertIn("重新打开账号页", text)
        self.assertIn('href="/"', text)
        self.assertIn("重新登录此账号", text)
        self.store.select.assert_not_called()

    async def test_page_distinguishes_selection_from_reauthorization(self):
        response = await self.client.get("/", headers=self.headers)
        text = await response.text()
        self.assertEqual(response.status, 200)
        self.assertIn("重新登录此账号", text)
        self.assertIn("仅切换已保存的账号", text)
        self.assertIn("/authorize?account=example", text)
        self.assertNotIn("Sol", text)
        self.assertEqual(response.headers["Referrer-Policy"], "same-origin")
        self.assertIn("当前账号已启用", text)
        self.assertNotIn('action="/select"', text)


if __name__ == "__main__":
    unittest.main()

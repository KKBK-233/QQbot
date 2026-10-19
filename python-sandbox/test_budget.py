"""验证持久预算、身份隔离、失败熔断及审计最小化。"""

from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
import json
from pathlib import Path
import tempfile
import unittest

from budget import Budget, BudgetDenied


class BudgetTests(unittest.TestCase):
    """时间显式注入，测试不消耗真实机器人预算。"""

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.budget = Budget(self.temp.name)

    def tearDown(self):
        self.temp.cleanup()

    def admit(self, scope='s', actor='u', now=1000000, status=True):
        token = self.budget.reserve(scope, actor, 'secret-code', 100, now=now)
        self.budget.finish(token, {'success': status}, {'cpu_ms': 12, 'wall_ms': 20})
        return token

    def test_restart_does_not_reset(self):
        self.admit()
        again = Budget(self.temp.name)
        with self.assertRaises(BudgetDenied):
            again.reserve('s', 'u', 'x', 1, now=1000001)

    def test_unknown_context_denied(self):
        with self.assertRaises(BudgetDenied):
            self.budget.reserve('', '', 'x', 1)

    def test_global_minute(self):
        for i in range(6):
            self.admit(str(i), str(i))
        with self.assertRaisesRegex(BudgetDenied, '一分钟'):
            self.admit('other', 'other', now=1000005)

    def test_scope_and_actor_minute(self):
        self.admit(now=1000000)
        self.admit(now=1000011)
        with self.assertRaisesRegex(BudgetDenied, '一分钟'):
            self.admit(now=1000022)
        with self.assertRaisesRegex(BudgetDenied, '一分钟'):
            self.admit('new-scope', 'u', now=1000022)

    def test_daily_limits(self):
        for i in range(10):
            self.admit(str(i), 'u', now=1000000+i*61)
        with self.assertRaisesRegex(BudgetDenied, '24小时'):
            self.admit('new', 'u', now=1001000)
        for i in range(20):
            self.admit('shared', 'user'+str(i), now=1002000+i*61)
        with self.assertRaisesRegex(BudgetDenied, '24小时'):
            self.admit('shared', 'unused', now=1004000)
        for i in range(70):
            self.admit('scope'+str(i), 'actor'+str(i), now=1005000+i*61)
        with self.assertRaisesRegex(BudgetDenied, '24小时'):
            self.admit('unused-scope', 'unused', now=1010000)

    def test_failure_breaker(self):
        for i in range(3):
            self.admit(now=1000000+i*61, status=False)
        with self.assertRaisesRegex(BudgetDenied, '暂停15分钟'):
            self.admit(now=1000200)
        self.admit(now=1001100)

    def test_pending_crash_keeps_charge(self):
        self.budget.reserve('s', 'u', 'x', 1, now=1000000)
        self.assertRaises(BudgetDenied, Budget(self.temp.name).reserve, 's', 'u', 'x', 1, now=1000001)

    def test_clock_rollback(self):
        self.admit(now=1000000)
        with self.assertRaises(BudgetDenied):
            self.admit(now=999000)

    def test_concurrent_reservation_is_atomic(self):
        def attempt(i):
            try:
                self.admit('same', 'same', now=1000000)
                return True
            except BudgetDenied:
                return False
        with ThreadPoolExecutor(max_workers=6) as pool:
            self.assertEqual(sum(pool.map(attempt, range(6))), 1)

    def test_no_code_or_raw_identity_in_audit(self):
        self.admit('private-chat-id', 'private-user-id')
        with closing(self.budget.connect()) as db:
            rows = db.execute('SELECT * FROM jobs').fetchall()
        text = json.dumps(rows)
        for secret in ('secret-code', 'private-chat-id', 'private-user-id'):
            self.assertNotIn(secret, text)


if __name__ == '__main__':
    unittest.main()

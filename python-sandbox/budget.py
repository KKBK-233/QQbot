"""持久预付预算与最小审计；执行失败、取消或重启均不返还额度。"""

from contextlib import closing
import hashlib
from pathlib import Path
import sqlite3
import time
import uuid

STATE = Path('/home/qqbot/QQbot/MaiBot/data/plugins/local.qqbot-python-sandbox')
CPU_RESERVE_MS = 4000


class BudgetDenied(ValueError):
    """预算或熔断拒绝，必须停止自动重试。"""


class Budget:
    """数据库事务使配额跨进程、插件重载和服务重启持续生效。"""

    def __init__(self, root=STATE):
        self.root = Path(root)
        self.root.mkdir(mode=0o700, parents=True, exist_ok=True)
        self.path = self.root / 'usage.sqlite3'
        with closing(self.connect()) as db:
            db.execute('PRAGMA auto_vacuum=FULL')
            db.execute('CREATE TABLE IF NOT EXISTS jobs (id TEXT PRIMARY KEY, started REAL, scope TEXT, actor TEXT, '
                       'code_sha256 TEXT, input_bytes INTEGER, status TEXT, cpu_ms INTEGER DEFAULT 0, '
                       'wall_ms INTEGER DEFAULT 0, memory_peak_bytes INTEGER DEFAULT 0, output_bytes INTEGER DEFAULT 0)')
            db.commit()
        self.path.chmod(0o600)

    def connect(self):
        """限制审计数据库最多2MiB，满盘/锁冲突时拒绝执行。"""
        db = sqlite3.connect(self.path, timeout=1)
        db.execute('PRAGMA max_page_count=512')
        return db

    def reserve(self, scope, actor, code, input_bytes, *, now=None):
        """按滚动24小时预扣4 CPU秒/次；只记录哈希与计量，不记录代码或文件正文。"""
        if not scope:
            raise BudgetDenied('缺少可信会话上下文，拒绝执行')
        now = time.time() if now is None else now
        scope_hash = hashlib.sha256(scope.encode()).hexdigest()
        actor_hash = hashlib.sha256((actor or 'unknown').encode()).hexdigest()
        with closing(self.connect()) as db:
            db.execute('BEGIN IMMEDIATE')
            db.execute('DELETE FROM jobs WHERE started < ?', (now - 8 * 86400,))
            rows = db.execute('SELECT started,scope,actor,status FROM jobs WHERE started>? ORDER BY started DESC',
                              (now - 86400,)).fetchall()
            # 系统时钟回拨不清空预算，也不使短时限流失效。
            recent = [r for r in rows if r[0] > now - 60]
            mine = [r for r in rows if r[1] == scope_hash]
            user = [r for r in rows if r[2] == actor_hash]
            if len(rows) >= 100 or len(mine) >= 20 or len(user) >= 10:
                raise BudgetDenied('滚动24小时预算已用尽，停止重试')
            if len(recent) >= 6 or sum(r[1] == scope_hash for r in recent) >= 2 or sum(r[2] == actor_hash for r in recent) >= 2:
                raise BudgetDenied('一分钟调用次数已达上限，停止重试')
            if mine and mine[0][0] > now - 10:
                raise BudgetDenied('同会话冷却10秒，停止重试')
            if len(mine) >= 3 and all(r[3] != 'ok' for r in mine[:3]) and mine[0][0] > now - 900:
                raise BudgetDenied('同会话连续3次失败，暂停15分钟，停止重试')
            job_id = uuid.uuid4().hex
            db.execute('INSERT INTO jobs (id,started,scope,actor,code_sha256,input_bytes,status) VALUES (?,?,?,?,?,?,?)',
                       (job_id, now, scope_hash, actor_hash, hashlib.sha256(code.encode()).hexdigest(), input_bytes, 'pending'))
            db.commit()
            return job_id

    def finish(self, job_id, result, usage):
        """只持久化宿主采样的指标，未完成的任务也已扣预算。"""
        with closing(self.connect()) as db:
            db.execute('UPDATE jobs SET status=?,cpu_ms=?,wall_ms=?,memory_peak_bytes=?,output_bytes=? WHERE id=?',
                       ('ok' if result.get('success') else 'failed', usage.get('cpu_ms', 0), usage.get('wall_ms', 0),
                        usage.get('memory_peak_bytes', 0), usage.get('output_bytes', 0), job_id))
            db.commit()

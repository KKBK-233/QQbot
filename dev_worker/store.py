"""持久任务状态与单 worker 锁，代理无法接触账本目录。"""
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
import json
import os
import re
import sqlite3
import tempfile
import uuid

TERMINAL = frozenset({'succeeded', 'failed', 'timeout', 'cancelled'})
TASK_ID = re.compile(r'\d{8}T\d{6}Z-[0-9a-f]{12}\Z')

def now():
    return datetime.now(timezone.utc).isoformat()

def atomic_json(path: Path, value):
    """产物写入采用原子替换，读者不会看到半截 JSON。"""
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, name = tempfile.mkstemp(dir=path.parent, prefix='.write-')
    try:
        with os.fdopen(fd, 'w', encoding='utf-8') as stream:
            json.dump(value, stream, ensure_ascii=False, indent=2)
            stream.write('\n')
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(name, path)
    finally:
        Path(name).unlink(missing_ok=True)

@contextmanager
def exclusive_lock(path: Path):
    """内核文件锁随进程退出释放；不依赖可过期或复用的 PID。"""
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open('a+b') as stream:
        if os.name == 'nt':
            import msvcrt
            stream.write(b'0'); stream.flush(); stream.seek(0)
            msvcrt.locking(stream.fileno(), msvcrt.LK_NBLCK, 1)
        else:
            import fcntl
            fcntl.flock(stream, fcntl.LOCK_EX | fcntl.LOCK_NB)
        try:
            yield
        finally:
            if os.name == 'nt':
                stream.seek(0)
                msvcrt.locking(stream.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                fcntl.flock(stream, fcntl.LOCK_UN)

class Store:
    """SQLite 事务协调 submit/cancel/run；终态只能写入一次。"""
    def __init__(self, root: Path):
        self.root = root.resolve()
        self.root.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.path = self.root/'worker.sqlite3'
        with self.connect() as db:
            db.execute('''CREATE TABLE IF NOT EXISTS tasks (
                id TEXT PRIMARY KEY, spec TEXT NOT NULL, status TEXT NOT NULL,
                phase TEXT NOT NULL, cancel_requested INTEGER NOT NULL DEFAULT 0,
                created_at TEXT NOT NULL, started_at TEXT, finished_at TEXT, reason TEXT,
                result TEXT NOT NULL DEFAULT '{}')''')

    @contextmanager
    def connect(self):
        db = sqlite3.connect(self.path, timeout=10)
        db.row_factory = sqlite3.Row
        try:
            with db:
                yield db
        finally:
            db.close()

    def directory(self, task_id):
        if not isinstance(task_id, str) or not TASK_ID.fullmatch(task_id):
            raise ValueError('task_id 格式错误')
        base = self.root/'tasks'
        target = base/task_id
        if base.is_symlink() or target.is_symlink() or target.resolve().parent != base.resolve():
            raise ValueError('任务目录越界')
        return target

    def submit(self, spec):
        task_id = datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ-')+uuid.uuid4().hex[:12]
        folder = self.directory(task_id)
        folder.mkdir(parents=True, mode=0o700)
        with self.connect() as db:
            db.execute('INSERT INTO tasks(id,spec,status,phase,created_at) VALUES(?,?,?,?,?)',
                       (task_id, json.dumps(spec, ensure_ascii=False), 'queued', 'queued', now()))
        return self.get(task_id)

    def get(self, task_id):
        self.directory(task_id)
        with self.connect() as db:
            row = db.execute('SELECT * FROM tasks WHERE id=?', (task_id,)).fetchone()
        if row is None: raise ValueError('任务不存在')
        result = dict(row)
        result['spec'] = json.loads(result['spec'])
        result['result'] = json.loads(result['result'])
        result['cancel_requested'] = bool(result['cancel_requested'])
        return result

    def list(self):
        with self.connect() as db:
            rows = db.execute('SELECT id,status,phase,created_at,reason FROM tasks ORDER BY created_at DESC LIMIT 100').fetchall()
        return [dict(row) for row in rows]

    def claim(self):
        with self.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            row = db.execute("SELECT id FROM tasks WHERE status='queued' ORDER BY created_at,id LIMIT 1").fetchone()
            if not row: return None
            db.execute("UPDATE tasks SET status='running',phase='prepare',started_at=? WHERE id=?",
                       (now(),row['id']))
        return self.get(row['id'])

    def phase(self, task_id, phase):
        with self.connect() as db:
            db.execute("UPDATE tasks SET phase=? WHERE id=? AND status='running'", (phase,task_id))

    def finish(self, task_id, status, reason, result=None):
        if status not in TERMINAL: raise ValueError('非法终态')
        with self.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            row=db.execute('SELECT status,cancel_requested FROM tasks WHERE id=?',(task_id,)).fetchone()
            if row is None: raise ValueError('任务不存在')
            if row['status'] in TERMINAL: return False
            if row['cancel_requested']:
                status,reason='cancelled','cancelled'
            db.execute("UPDATE tasks SET status=?,phase='done',reason=?,finished_at=?,result=? WHERE id=?",
                       (status,reason,now(),json.dumps(result or {},ensure_ascii=False),task_id))
        return True

    def cancel(self, task_id):
        self.get(task_id)
        with self.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            state = db.execute('SELECT status FROM tasks WHERE id=?',(task_id,)).fetchone()['status']
            if state == 'queued':
                db.execute("UPDATE tasks SET status='cancelled',phase='done',reason='cancelled_before_start',cancel_requested=1,finished_at=? WHERE id=?",(now(),task_id))
            elif state == 'running':
                db.execute('UPDATE tasks SET cancel_requested=1 WHERE id=?',(task_id,))
        return self.get(task_id)

    def recover(self, project=lambda value:value):
        """只由取得 worker 锁的进程调用，不自动重跑可能已经产生副作用的任务。"""
        with self.connect() as db:
            rows = db.execute("SELECT id FROM tasks WHERE status='running'").fetchall()
        for row in rows:
            self.finish(row['id'],'failed','worker_interrupted')
            atomic_json(self.directory(row['id'])/'summary.json',project(self.get(row['id'])))
        return len(rows)

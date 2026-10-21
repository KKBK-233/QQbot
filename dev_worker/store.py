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
            db.execute('CREATE TABLE IF NOT EXISTS task_requests (request_key TEXT PRIMARY KEY, task_id TEXT NOT NULL UNIQUE)')
            db.execute('''CREATE TABLE IF NOT EXISTS notifications (
                id TEXT PRIMARY KEY, task_id TEXT NOT NULL UNIQUE, session_id TEXT NOT NULL,
                state TEXT NOT NULL DEFAULT 'pending', claim_token TEXT, receipt TEXT,
                created_at TEXT NOT NULL, sent_at TEXT)''')
            db.execute('''CREATE TABLE IF NOT EXISTS development_sessions (
                root_task_id TEXT PRIMARY KEY, current_task_id TEXT NOT NULL)''')
            db.execute('''CREATE TABLE IF NOT EXISTS development_inputs (
                request_key TEXT PRIMARY KEY, root_task_id TEXT NOT NULL,
                task_id TEXT NOT NULL, content TEXT NOT NULL, created_at TEXT NOT NULL)''')

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

    def submit(self, spec, request_key=None):
        task_id = datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ-')+uuid.uuid4().hex[:12]
        folder = self.directory(task_id)
        with self.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            if request_key:
                existing=db.execute('SELECT task_id FROM task_requests WHERE request_key=?',(request_key,)).fetchone()
                if existing: return self.get(existing['task_id'])
            folder.mkdir(parents=True, mode=0o700)
            db.execute('INSERT INTO tasks(id,spec,status,phase,created_at) VALUES(?,?,?,?,?)',
                       (task_id, json.dumps(spec, ensure_ascii=False), 'queued', 'queued', now()))
            if request_key: db.execute('INSERT INTO task_requests VALUES(?,?)',(request_key,task_id))
        return self.get(task_id)

    def get_request(self, request_key):
        with self.connect() as db:
            row=db.execute('SELECT task_id FROM task_requests WHERE request_key=?',(request_key,)).fetchone()
        return self.get(row['task_id']) if row else None

    def session(self, task_id):
        """任务轮次共享一个稳定开发会话，旧终态和旧产物保持不变。"""
        task = self.get(task_id)
        root = task['spec'].get('session_root', task_id)
        with self.connect() as db:
            row = db.execute('SELECT current_task_id FROM development_sessions WHERE root_task_id=?', (root,)).fetchone()
        return root, self.get(row['current_task_id'] if row else root)

    def follow_up(self, task_id, content, request_key, origin):
        """串行追加到待执行轮次；已运行的轮次不被中断，也不重新打开终态。"""
        root, _ = self.session(task_id)
        if not isinstance(content, str) or not content.strip() or len(content.encode('utf-8')) > 8192:
            raise ValueError('会话补充必须为有界非空文本')
        with self.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            previous = db.execute('SELECT task_id FROM development_inputs WHERE request_key=?', (request_key,)).fetchone()
            if previous:
                selected = previous['task_id']
            else:
                db.execute('INSERT OR IGNORE INTO development_sessions VALUES(?,?)', (root, root))
                current_id = db.execute('SELECT current_task_id FROM development_sessions WHERE root_task_id=?', (root,)).fetchone()[0]
                row = db.execute('SELECT * FROM tasks WHERE id=?', (current_id,)).fetchone()
                selected = current_id
                if row['status'] != 'queued':
                    selected = datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ-') + uuid.uuid4().hex[:12]
                    folder = self.directory(selected)
                    spec = json.loads(row['spec'])
                    spec.update(session_root=root, continuation_of=current_id, origin=origin,
                                task='继续同一开发会话，沿用原目标、工作副本和已执行结果。')
                    folder.mkdir(parents=True, mode=0o700)
                    db.execute('INSERT INTO tasks(id,spec,status,phase,created_at) VALUES(?,?,?,?,?)',
                               (selected, json.dumps(spec, ensure_ascii=False), 'queued', 'queued', now()))
                    db.execute('UPDATE development_sessions SET current_task_id=? WHERE root_task_id=?', (selected, root))
                count, size = db.execute('SELECT count(*),coalesce(sum(length(cast(content AS BLOB))),0) FROM development_inputs WHERE task_id=?', (selected,)).fetchone()
                if count >= 8 or size + len(content.encode('utf-8')) > 32768:
                    raise ValueError('本轮补充消息已达上限，请等待当前轮次完成')
                db.execute('INSERT INTO development_inputs VALUES(?,?,?,?,?)', (request_key, root, selected, content, now()))
                # 新补充决定待执行轮次的通知群；不改原始归属，也不移动已发送回执。
                selected_spec=json.loads(db.execute('SELECT spec FROM tasks WHERE id=?',(selected,)).fetchone()[0])
                selected_spec['notification_origin']={key:origin[key] for key in
                    ('platform','session_id','group_id','user_id','message_id')}
                db.execute('UPDATE tasks SET spec=? WHERE id=?',
                           (json.dumps(selected_spec,ensure_ascii=False),selected))
        return self.get(selected)

    def inputs(self, task_id):
        with self.connect() as db:
            rows = db.execute('SELECT content FROM development_inputs WHERE task_id=? ORDER BY created_at,rowid', (task_id,)).fetchall()
        return [row['content'] for row in rows]

    def cancel_session(self, task_id):
        """会话取消覆盖正在执行及已排队的补充轮次，仍沿用既有进程回收。"""
        root, _ = self.session(task_id)
        with self.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            latest = db.execute('SELECT current_task_id FROM development_sessions WHERE root_task_id=?', (root,)).fetchone()
            current_id = latest[0] if latest else root
            rows = db.execute("SELECT id,spec FROM tasks WHERE status IN ('queued','running')").fetchall()
            for row in rows:
                if row['id'] == root or json.loads(row['spec']).get('session_root') == root:
                    db.execute("UPDATE tasks SET cancel_requested=1 WHERE id=?", (row['id'],))
                    db.execute("UPDATE tasks SET status='cancelled',phase='done',reason='cancelled_before_start',finished_at=? WHERE id=? AND status='queued'",
                               (now(),row['id']))
        return self.get(current_id)

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

    def queue_notifications(self):
        """终态回执使用持久 outbox；不为本地 CLI 任务生成群通知。"""
        with self.connect() as db:
            rows=db.execute("SELECT id,spec FROM tasks WHERE status IN ('succeeded','failed','timeout','cancelled') AND id NOT IN (SELECT task_id FROM notifications)").fetchall()
            for row in rows:
                spec=json.loads(row['spec'])
                origin=spec.get('notification_origin') or spec.get('origin',{})
                if not origin.get('session_id'): continue
                db.execute('INSERT OR IGNORE INTO notifications(id,task_id,session_id,created_at) VALUES(?,?,?,?)',
                           (uuid.uuid4().hex,row['id'],origin['session_id'],now()))

    def take_notification(self, session_id):
        """先标记发送中再交付；发送结果不确定时不自动重发。"""
        with self.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            row=db.execute("SELECT * FROM notifications WHERE session_id=? AND state='pending' ORDER BY created_at,id LIMIT 1",(session_id,)).fetchone()
            if row is None: return None
            token=uuid.uuid4().hex
            db.execute("UPDATE notifications SET state='sending',claim_token=? WHERE id=?",(token,row['id']))
        return {**dict(row),'state':'sending','claim_token':token}

    def acknowledge_notification(self, session_id, notification_id, token, receipt):
        """真实发送回执单独入库；重试同一确认不会再次发消息。"""
        if not isinstance(receipt,str) or not receipt or len(receipt)>256: raise ValueError('缺少真实发送回执')
        with self.connect() as db:
            row=db.execute('SELECT * FROM notifications WHERE id=? AND session_id=? AND claim_token=?',
                           (notification_id,session_id,token)).fetchone()
            if row is None: raise PermissionError('通知确认来源不符')
            if row['state']=='sent': return True
            db.execute("UPDATE notifications SET state='sent',receipt=?,sent_at=? WHERE id=?",
                       (receipt,now(),notification_id))
        return True

    def notification_state(self, task_id):
        with self.connect() as db:
            row=db.execute('SELECT state,receipt FROM notifications WHERE task_id=?',(task_id,)).fetchone()
        return dict(row) if row else None

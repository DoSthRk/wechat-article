"""站内修订与持久化任务。事务短锁不包含模型、PDF 渲染或远端发布。"""
from __future__ import annotations

from contextlib import contextmanager
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import sqlite3
import uuid

from utils.editorial_document import EditorialError
from utils.runtime_paths import resolve_runtime_dir


def now() -> str:
    return datetime.now(timezone.utc).isoformat()


def digest(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def encode(value) -> bytes:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(',', ':'), allow_nan=False).encode()


class Conflict(EditorialError):
    pass


class Store:
    def __init__(self, root: str | Path | None = None):
        self.root = Path(root or os.getenv('EDITORIAL_DATA_DIR') or
                         resolve_runtime_dir(str(Path(__file__).resolve().parent.parent)) / 'editorial')
        self.root.mkdir(parents=True, exist_ok=True, mode=0o700)
        with self.connection() as db:
            db.executescript('''
                PRAGMA journal_mode=WAL;
                CREATE TABLE IF NOT EXISTS heads (
                    job_pk INTEGER PRIMARY KEY, job_id TEXT NOT NULL, line TEXT NOT NULL,
                    revision TEXT NOT NULL, source_hash TEXT NOT NULL, updated TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS revisions (
                    id TEXT PRIMARY KEY, job_pk INTEGER NOT NULL, parent TEXT,
                    author TEXT NOT NULL, created TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS uploads (
                    line TEXT NOT NULL, source_hash TEXT NOT NULL, job_pk INTEGER NOT NULL,
                    UNIQUE(line, source_hash));
                CREATE TABLE IF NOT EXISTS tasks (
                    id TEXT PRIMARY KEY, kind TEXT NOT NULL, job_pk INTEGER NOT NULL,
                    revision TEXT NOT NULL, status TEXT NOT NULL, payload TEXT NOT NULL,
                    created TEXT NOT NULL, updated TEXT NOT NULL,
                    UNIQUE(kind, job_pk, revision));
                CREATE INDEX IF NOT EXISTS editorial_task_state ON tasks(kind, status);
                CREATE TABLE IF NOT EXISTS inbox_archives (
                    job_pk INTEGER PRIMARY KEY, line TEXT NOT NULL, active INTEGER NOT NULL,
                    snapshot TEXT NOT NULL, updated TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS inbox_archive_events (
                    id TEXT PRIMARY KEY, job_pk INTEGER NOT NULL, active INTEGER NOT NULL,
                    author TEXT NOT NULL, created TEXT NOT NULL, snapshot TEXT NOT NULL);
            ''')
        os.chmod(self.root / 'editorial.sqlite3', 0o600)

    @contextmanager
    def connection(self, write=False):
        db = sqlite3.connect(str(self.root / 'editorial.sqlite3'), timeout=10)
        db.row_factory = sqlite3.Row
        try:
            if write: db.execute('BEGIN IMMEDIATE')
            yield db
            db.commit()
        except Exception:
            db.rollback()
            raise
        finally:
            db.close()

    def directory(self, job_pk: int) -> Path:
        if type(job_pk) is not int or job_pk < 1: raise EditorialError('文章记录无效')
        path = self.root / 'articles' / str(job_pk)
        path.mkdir(parents=True, exist_ok=True)
        return path

    def put(self, job_pk: int, data: bytes, suffix: str) -> str:
        if suffix not in {'.json', '.jpg', '.png', '.pdf', '.md'}: raise EditorialError('资产类型无效')
        name = digest(data) + suffix
        path = self.directory(job_pk) / name
        if not path.exists():
            temporary = path.with_name('.' + uuid.uuid4().hex)
            try:
                temporary.write_bytes(data)
                os.chmod(temporary, 0o600)
                temporary.replace(path)
            finally:
                temporary.unlink(missing_ok=True)
        return name

    def asset(self, job_pk: int, name: str) -> Path:
        import re
        if not isinstance(name, str) or not re.fullmatch(r'[0-9a-f]{64}\.(pdf|png|jpg|json|md)', name):
            raise EditorialError('资产标识无效')
        path = self.directory(job_pk) / name
        if not path.is_file() or path.is_symlink() or digest(path.read_bytes()) != name.split('.')[0]:
            raise EditorialError('资产缺失或内容已变化')
        return path

    def head(self, job_pk: int) -> dict | None:
        with self.connection() as db:
            row = db.execute('SELECT * FROM heads WHERE job_pk=?', (job_pk,)).fetchone()
            return dict(row) if row else None

    def revision(self, job_pk: int, revision: str) -> dict:
        with self.connection() as db:
            row = db.execute('SELECT * FROM revisions WHERE id=? AND job_pk=?', (revision, job_pk)).fetchone()
        if not row: raise EditorialError('找不到这篇文章的修订版本')
        value = json.loads(self.asset(job_pk, revision + '.json').read_text())
        return {**value, 'revision': revision, 'author': row['author'], 'created': row['created']}

    def save(self, job_pk: int, job_id: str, line: str, payload: dict, *, expected: str | None, author: str) -> dict:
        payload = {k: v for k, v in payload.items() if k not in {'revision', 'author', 'created'}}
        payload.update(job_pk=job_pk, revision_nonce=uuid.uuid4().hex)
        name = self.put(job_pk, encode(payload), '.json')
        revision = name[:-5]
        with self.connection(write=True) as db:
            self._require_inbox(db, job_pk)
            head = db.execute('SELECT * FROM heads WHERE job_pk=?', (job_pk,)).fetchone()
            if (head['revision'] if head else None) != expected:
                raise Conflict('另一位同事已保存了新版本。您的修改仍在页面中，请先核对最新版本。')
            db.execute('INSERT OR IGNORE INTO revisions VALUES(?,?,?,?,?)', (revision, job_pk, expected, author, now()))
            db.execute('INSERT INTO heads VALUES(?,?,?,?,?,?) ON CONFLICT(job_pk) DO UPDATE SET revision=excluded.revision,updated=excluded.updated',
                       (job_pk, job_id, line, revision, payload['source_hash'], now()))
        return self.revision(job_pk, revision)

    def history(self, job_pk: int) -> list:
        with self.connection() as db:
            return [dict(r) for r in db.execute('SELECT id,author,created,parent FROM revisions WHERE job_pk=? ORDER BY created DESC LIMIT 100', (job_pk,))]

    def enqueue(self, kind: str, job_pk: int, revision: str, payload: dict) -> dict:
        with self.connection(write=True) as db:
            self._require_inbox(db, job_pk)
            existing = db.execute('SELECT * FROM tasks WHERE kind=? AND job_pk=? AND revision=?', (kind, job_pk, revision)).fetchone()
            if existing: return self._task(existing)
            if kind == 'publish':
                head = db.execute('SELECT revision FROM heads WHERE job_pk=?', (job_pk,)).fetchone()
                if not head or head['revision'] != revision:
                    raise Conflict('审核期间草稿已变化，请重新预览后发布')
            if kind == 'publish' and db.execute("SELECT id FROM tasks WHERE job_pk=? AND kind='publish' AND status IN ('queued','running','uncertain')", (job_pk,)).fetchone():
                raise Conflict('这篇文章还有正在发布或结果待核对的版本，请先处理原发布任务。')
            task_id = uuid.uuid4().hex
            db.execute('INSERT INTO tasks VALUES(?,?,?,?,?,?,?,?)',
                       (task_id, kind, job_pk, revision, 'queued', encode(payload).decode(), now(), now()))
        return self.task(task_id)

    @staticmethod
    def _task(row) -> dict:
        result = dict(row)
        result['payload'] = json.loads(result['payload'])
        return result

    def task(self, task_id: str) -> dict:
        with self.connection() as db:
            row = db.execute('SELECT * FROM tasks WHERE id=?', (task_id,)).fetchone()
        if not row: raise EditorialError('任务不存在')
        return self._task(row)

    def tasks(self, kind: str | None = None) -> list:
        with self.connection() as db:
            query = 'SELECT * FROM tasks' + (' WHERE kind=?' if kind else '') + ' ORDER BY created DESC'
            return [self._task(r) for r in db.execute(query, (kind,) if kind else ())]

    def update_task(self, task_id: str, *, status: str, payload: dict) -> None:
        with self.connection(write=True) as db:
            db.execute('UPDATE tasks SET status=?,payload=?,updated=? WHERE id=?', (status, encode(payload).decode(), now(), task_id))

    def retry(self, task_id: str, target: str | None = None) -> dict:
        with self.connection(write=True) as db:
            row = db.execute('SELECT * FROM tasks WHERE id=?', (task_id,)).fetchone()
            if not row: raise EditorialError('任务不存在')
            task = self._task(row)
            self._require_inbox(db, task['job_pk'])
            if task['status'] not in {'failed', 'partial'}: raise Conflict('此任务不能重试，请先核对当前结果')
            payload = task['payload']
            if task['kind'] == 'publish':
                newer = db.execute("SELECT id FROM tasks WHERE job_pk=? AND kind='publish' AND created>?", (task['job_pk'],task['created'])).fetchone()
                if newer: raise Conflict('已有更新的发布版本，不能重试旧版本覆盖当前文章')
                item = payload.get('targets', {}).get(target)
                if not item or item['status'] != 'failed': raise Conflict('只能重试明确失败的渠道')
                item['status'] = 'pending'
                item.pop('error', None)
            payload.pop('error', None)
            db.execute("UPDATE tasks SET status='queued',payload=?,updated=? WHERE id=?", (encode(payload).decode(), now(), task_id))
        return self.task(task_id)

    @staticmethod
    def _require_inbox(db, job_pk):
        if db.execute('SELECT job_pk FROM inbox_archives WHERE job_pk=? AND active=1',(job_pk,)).fetchone():
            raise Conflict('此草稿已移出草稿箱，请先恢复后再编辑或发布')

    def archived_jobs(self, line):
        with self.connection() as db:
            return {row['job_pk'] for row in db.execute('SELECT job_pk FROM inbox_archives WHERE line=? AND active=1',(line,))}

    def set_inbox_archived(self, job_pk, line, archived, *, expected, author, snapshot):
        """只改变草稿箱可见性，保留正文、PDF、修订、任务及发布回执。"""
        with self.connection(write=True) as db:
            head=db.execute('SELECT revision FROM heads WHERE job_pk=?',(job_pk,)).fetchone()
            if (head['revision'] if head else None) != expected:
                raise Conflict('草稿已被修改，请刷新列表后再操作')
            if db.execute("SELECT id FROM tasks WHERE job_pk=? AND status IN ('queued','running','uncertain')",(job_pk,)).fetchone():
                raise Conflict('这篇文章仍有任务处理中或结果待核对，暂不能移出草稿箱')
            current=db.execute('SELECT active FROM inbox_archives WHERE job_pk=?',(job_pk,)).fetchone()
            if current and bool(current['active']) == archived: return
            stamp=now(); saved=encode(snapshot).decode()
            db.execute('INSERT INTO inbox_archives VALUES(?,?,?,?,?) ON CONFLICT(job_pk) DO UPDATE SET active=excluded.active,snapshot=excluded.snapshot,updated=excluded.updated',
                       (job_pk,line,int(archived),saved,stamp))
            db.execute('INSERT INTO inbox_archive_events VALUES(?,?,?,?,?,?)',
                       (uuid.uuid4().hex,job_pk,int(archived),author,stamp,saved))

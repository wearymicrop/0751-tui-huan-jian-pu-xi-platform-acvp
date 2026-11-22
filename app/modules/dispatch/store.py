"""处置任务的追加式存储。

派单只追加修订，不覆盖现场已收到的任务。所有写入都用 ``BEGIN IMMEDIATE``
串行化；修订与任务按主键去重，重跑和续跑都不会重复派单。
"""
import json
import sqlite3
import threading
from contextlib import contextmanager

from app.config import database_path

_INITIALIZED = set()
_INIT_LOCK = threading.Lock()

SCHEMA = """
CREATE TABLE IF NOT EXISTS dispatches (
    id TEXT PRIMARY KEY,
    batch TEXT NOT NULL,
    start TEXT,
    end TEXT,
    source TEXT NOT NULL,
    seq INTEGER NOT NULL DEFAULT 0,
    created TEXT NOT NULL,
    updated TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS dispatch_inputs (
    dispatch_id TEXT NOT NULL,
    seq INTEGER NOT NULL,
    kind TEXT NOT NULL,
    digest TEXT NOT NULL,
    detail TEXT NOT NULL,
    created TEXT NOT NULL,
    PRIMARY KEY (dispatch_id, seq)
);
CREATE TABLE IF NOT EXISTS dispatch_supplements (
    dispatch_id TEXT NOT NULL,
    digest TEXT NOT NULL,
    kind TEXT NOT NULL,
    seq INTEGER NOT NULL,
    detail TEXT NOT NULL,
    created TEXT NOT NULL,
    PRIMARY KEY (dispatch_id, digest)
);
CREATE TABLE IF NOT EXISTS dispatch_revisions (
    dispatch_id TEXT NOT NULL,
    seq INTEGER NOT NULL,
    revision_id TEXT NOT NULL,
    basis TEXT NOT NULL,
    plan TEXT NOT NULL,
    created TEXT NOT NULL,
    PRIMARY KEY (dispatch_id, seq)
);
CREATE TABLE IF NOT EXISTS dispatch_tasks (
    dispatch_id TEXT NOT NULL,
    seq INTEGER NOT NULL,
    task_id TEXT NOT NULL,
    serial TEXT NOT NULL,
    payload TEXT NOT NULL,
    PRIMARY KEY (dispatch_id, seq, task_id)
);
CREATE TABLE IF NOT EXISTS dispatch_jobs (
    dispatch_id TEXT NOT NULL,
    job_id TEXT NOT NULL,
    status TEXT NOT NULL,
    kind TEXT NOT NULL,
    input_digest TEXT NOT NULL,
    payload TEXT,
    error TEXT,
    revision_seq INTEGER,
    attempts INTEGER NOT NULL DEFAULT 0,
    created TEXT NOT NULL,
    updated TEXT NOT NULL,
    PRIMARY KEY (dispatch_id, job_id)
);
CREATE INDEX IF NOT EXISTS dispatch_jobs_status
    ON dispatch_jobs(dispatch_id, status, updated);
"""


@contextmanager
def connection(immediate=False):
    path = database_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    db = sqlite3.connect(path, timeout=30, isolation_level=None)
    db.row_factory = sqlite3.Row
    try:
        db.execute("PRAGMA busy_timeout=30000")
        _ensure_schema(path, db)
        if immediate:
            db.execute("BEGIN IMMEDIATE")
            try:
                yield db
            except BaseException:
                db.rollback()
                raise
            else:
                db.commit()
        else:
            yield db
    finally:
        db.close()


def _ensure_schema(path, db):
    """建表只做一次；并发连接不需要互相争抢 schema 写锁。"""
    key = str(path.resolve())
    with _INIT_LOCK:
        if key in _INITIALIZED:
            return
        db.execute("PRAGMA journal_mode=WAL")
        db.executescript(SCHEMA)
        _INITIALIZED.add(key)


def _loads(value):
    return json.loads(value) if value is not None else None


def _dump(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


# ---- 下发批次 -------------------------------------------------------------

def load_dispatch(db, dispatch_id):
    row = db.execute("SELECT * FROM dispatches WHERE id = ?", (dispatch_id,)).fetchone()
    if row is None:
        return None
    record = dict(row)
    record["source"] = json.loads(record["source"])
    return record


def insert_dispatch(db, dispatch_id, query, source, stamp):
    db.execute(
        "INSERT INTO dispatches(id, batch, start, end, source, seq, created, updated) "
        "VALUES (?, ?, ?, ?, ?, 0, ?, ?) ON CONFLICT(id) DO NOTHING",
        (dispatch_id, query["batch"], query["start"], query["end"], _dump(source), stamp, stamp),
    )
    return load_dispatch(db, dispatch_id)


def update_source(db, dispatch_id, source, stamp):
    db.execute("UPDATE dispatches SET source = ?, updated = ? WHERE id = ?",
               (_dump(source), stamp, dispatch_id))


def update_seq(db, dispatch_id, seq, stamp):
    db.execute("UPDATE dispatches SET seq = ?, updated = ? WHERE id = ?",
               (seq, stamp, dispatch_id))


def list_dispatches(db, limit=30):
    rows = db.execute("SELECT * FROM dispatches ORDER BY updated DESC, created DESC LIMIT ?",
                      (limit,)).fetchall()
    return [dict(row) for row in rows]


# ---- 输入台账 -------------------------------------------------------------

def record_input(db, dispatch_id, kind, digest, detail, stamp):
    row = db.execute("SELECT COALESCE(MAX(seq), 0) FROM dispatch_inputs WHERE dispatch_id = ?",
                     (dispatch_id,)).fetchone()
    seq = row[0] + 1
    db.execute(
        "INSERT INTO dispatch_inputs(dispatch_id, seq, kind, digest, detail, created) "
        "VALUES (?, ?, ?, ?, ?, ?)",
        (dispatch_id, seq, kind, digest, _dump(detail), stamp),
    )
    return seq


def list_inputs(db, dispatch_id, limit=60):
    rows = db.execute(
        "SELECT seq, kind, digest, detail, created FROM dispatch_inputs "
        "WHERE dispatch_id = ? ORDER BY seq DESC LIMIT ?",
        (dispatch_id, limit),
    ).fetchall()
    return [{**dict(row), "detail": json.loads(row["detail"])} for row in rows]


def has_supplement(db, dispatch_id, digest):
    return db.execute("SELECT 1 FROM dispatch_supplements WHERE dispatch_id = ? AND digest = ?",
                      (dispatch_id, digest)).fetchone() is not None


def record_supplement(db, dispatch_id, digest, kind, seq, detail, stamp):
    db.execute(
        "INSERT INTO dispatch_supplements(dispatch_id, digest, kind, seq, detail, created) "
        "VALUES (?, ?, ?, ?, ?, ?) ON CONFLICT(dispatch_id, digest) DO NOTHING",
        (dispatch_id, digest, kind, seq, _dump(detail), stamp),
    )


# ---- 修订 -----------------------------------------------------------------

def _revision(row):
    if row is None:
        return None
    record = dict(row)
    record["basis"] = json.loads(record["basis"])
    record["plan"] = json.loads(record["plan"])
    return record


def get_revision(db, dispatch_id, seq):
    row = db.execute("SELECT * FROM dispatch_revisions WHERE dispatch_id = ? AND seq = ?",
                     (dispatch_id, seq)).fetchone()
    return _revision(row)


def latest_revision(db, dispatch_id):
    row = db.execute(
        "SELECT * FROM dispatch_revisions WHERE dispatch_id = ? ORDER BY seq DESC LIMIT 1",
        (dispatch_id,),
    ).fetchone()
    return _revision(row)


def list_revisions(db, dispatch_id, limit=50):
    rows = db.execute(
        "SELECT * FROM dispatch_revisions WHERE dispatch_id = ? ORDER BY seq DESC LIMIT ?",
        (dispatch_id, limit),
    ).fetchall()
    return [_revision(row) for row in rows]


def insert_revision(db, dispatch_id, seq, revision_id, basis, plan, stamp):
    db.execute(
        "INSERT INTO dispatch_revisions(dispatch_id, seq, revision_id, basis, plan, created) "
        "VALUES (?, ?, ?, ?, ?, ?)",
        (dispatch_id, seq, revision_id, _dump(basis), _dump(plan), stamp),
    )


def insert_tasks(db, dispatch_id, seq, tasks):
    for task in tasks:
        db.execute(
            "INSERT INTO dispatch_tasks(dispatch_id, seq, task_id, serial, payload) "
            "VALUES (?, ?, ?, ?, ?) ON CONFLICT(dispatch_id, seq, task_id) DO NOTHING",
            (dispatch_id, seq, task["task_id"], task["serial"], _dump(task)),
        )


def revision_tasks(db, dispatch_id, seq):
    rows = db.execute(
        "SELECT payload FROM dispatch_tasks WHERE dispatch_id = ? AND seq = ? ORDER BY serial",
        (dispatch_id, seq),
    ).fetchall()
    return [json.loads(row["payload"]) for row in rows]


# ---- 作业（断点续跑） -----------------------------------------------------

def _job(row):
    if row is None:
        return None
    record = dict(row)
    record["payload"] = _loads(record["payload"])
    return record


def save_job(db, job):
    """登记一个待执行作业；同号作业只登记一次，已落地的不再回到待执行。"""
    db.execute(
        "INSERT INTO dispatch_jobs(dispatch_id, job_id, status, kind, input_digest, payload, "
        "error, revision_seq, attempts, created, updated) "
        "VALUES (?, ?, 'planned', ?, ?, ?, NULL, NULL, 0, ?, ?) "
        "ON CONFLICT(dispatch_id, job_id) DO NOTHING",
        (job["dispatch_id"], job["job_id"], job["kind"], job["input_digest"],
         _dump(job["payload"]) if job["payload"] is not None else None,
         job["created"], job["created"]),
    )
    return load_job(db, job["dispatch_id"], job["job_id"])


def load_job(db, dispatch_id, job_id):
    row = db.execute("SELECT * FROM dispatch_jobs WHERE dispatch_id = ? AND job_id = ?",
                     (dispatch_id, job_id)).fetchone()
    return _job(row)


def jobs(db, dispatch_id, limit=20):
    rows = db.execute(
        "SELECT job_id, status, kind, input_digest, error, revision_seq, attempts, created, updated "
        "FROM dispatch_jobs WHERE dispatch_id = ? ORDER BY rowid DESC LIMIT ?",
        (dispatch_id, limit),
    ).fetchall()
    return [dict(row) for row in rows]


def pending_jobs(db, dispatch_id):
    rows = db.execute(
        "SELECT * FROM dispatch_jobs WHERE dispatch_id = ? AND status IN ('planned', 'failed') "
        "ORDER BY rowid",
        (dispatch_id,),
    ).fetchall()
    return [_job(row) for row in rows]


def mark_job(db, dispatch_id, job_id, status, error, stamp, revision_seq=None):
    db.execute(
        "UPDATE dispatch_jobs SET status = ?, error = ?, updated = ?, attempts = attempts + 1, "
        "revision_seq = COALESCE(?, revision_seq), "
        "payload = CASE WHEN ? = 'applied' THEN NULL ELSE payload END "
        "WHERE dispatch_id = ? AND job_id = ?",
        (status, error, stamp, revision_seq, status, dispatch_id, job_id),
    )

"""SQLite のスキーマと最低限のヘルパー。"""
import json
import os
import sqlite3
import threading
import time

DB_PATH = os.environ.get("DB_PATH", "/data/harness.db")
_lock = threading.Lock()

SCHEMA = """
CREATE TABLE IF NOT EXISTS works (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    title TEXT NOT NULL,
    slug TEXT NOT NULL UNIQUE,
    request TEXT DEFAULT '',
    settings TEXT DEFAULT '',
    num_chapters INTEGER DEFAULT 3,
    stage TEXT DEFAULT 'draft',          -- draft/outlining/outline_review/generating/wait_q4/revising/done/failed
    claude_ok INTEGER DEFAULT 0,
    created_at REAL NOT NULL,
    updated_at REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS chapters (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    work_id INTEGER NOT NULL REFERENCES works(id),
    number INTEGER NOT NULL,
    title TEXT DEFAULT '',
    outline TEXT DEFAULT '',
    status TEXT DEFAULT 'outline',       -- outline/outlined/generating/generated/revising/final
    UNIQUE(work_id, number)
);
CREATE TABLE IF NOT EXISTS artifacts (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    work_id INTEGER NOT NULL,
    chapter_id INTEGER,
    kind TEXT NOT NULL,                  -- source/prompt/draft/summary/revised/final
    path TEXT NOT NULL,
    created_at REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS jobs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    work_id INTEGER NOT NULL,
    chapter_id INTEGER,
    kind TEXT NOT NULL,                  -- outline/generate_chapters/revise_all
    status TEXT DEFAULT 'queued',        -- queued/running/done/failed/cancelled
    payload TEXT DEFAULT '{}',
    error TEXT DEFAULT '',
    created_at REAL NOT NULL,
    updated_at REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS settings (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS glossary (
    term TEXT PRIMARY KEY,
    memo TEXT NOT NULL,
    created_at REAL NOT NULL,
    updated_at REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS notices (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    work_id INTEGER NOT NULL,
    body TEXT NOT NULL,
    created_at REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS reviews (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    work_id INTEGER NOT NULL,
    chapter_id INTEGER,
    source TEXT NOT NULL,                -- q4/muse/user
    body TEXT NOT NULL,
    created_at REAL NOT NULL
);
"""


def connect() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH, timeout=30)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    return conn


def init_db():
    os.makedirs(os.path.dirname(DB_PATH), exist_ok=True)
    with _lock, connect() as c:
        c.executescript(SCHEMA)
        cols = [r[1] for r in c.execute("PRAGMA table_info(works)").fetchall()]
        if "fanfic" not in cols:
            c.execute("ALTER TABLE works ADD COLUMN fanfic INTEGER DEFAULT 0")
            c.commit()
        if "mode" not in cols:
            c.execute("ALTER TABLE works ADD COLUMN mode TEXT DEFAULT 'switch'")
            c.commit()
        if "min_chars" not in cols:
            c.execute("ALTER TABLE works ADD COLUMN min_chars INTEGER DEFAULT 0")
            c.commit()
        jcols = [r[1] for r in c.execute("PRAGMA table_info(jobs)").fetchall()]
        if "usage" not in jcols:
            c.execute("ALTER TABLE jobs ADD COLUMN usage TEXT DEFAULT ''")
            c.commit()


def q(sql: str, args=()):
    with _lock, connect() as c:
        return [dict(r) for r in c.execute(sql, args).fetchall()]


def one(sql: str, args=()):
    rows = q(sql, args)
    return rows[0] if rows else None


def run(sql: str, args=()) -> int:
    with _lock, connect() as c:
        cur = c.execute(sql, args)
        c.commit()
        return cur.lastrowid


def now() -> float:
    return time.time()


def dumps(obj) -> str:
    return json.dumps(obj, ensure_ascii=False)


def get_setting(key: str, default: str = "") -> str:
    row = one("SELECT value FROM settings WHERE key=?", (key,))
    return row["value"] if row else default


def set_setting(key: str, value: str):
    run("INSERT INTO settings(key, value) VALUES(?,?) "
        "ON CONFLICT(key) DO UPDATE SET value=excluded.value", (key, value))


def get_int_setting(key: str, default: int) -> int:
    try:
        return int(get_setting(key, str(default)))
    except ValueError:
        return default


def get_float_setting(key: str, default: float) -> float:
    try:
        return float(get_setting(key, str(default)))
    except ValueError:
        return default

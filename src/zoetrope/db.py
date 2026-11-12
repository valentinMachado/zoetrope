"""SQLite storage shared by the API and the worker, which talk to each other only through it.

WAL mode lets the API read while the worker writes. Timestamps are Unix seconds (REAL).

Model: a job has keyframes (positions 0..N, position 0 is the start image) and segments
(segment i goes from keyframe i to keyframe i+1; a job with a single keyframe has one segment
with no end frame). Each render of a segment is a take; the user retains one take per segment
for the assembly. A job's status is derived from its takes, never stored.
"""

from __future__ import annotations

import json
import sqlite3
import time
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any

from . import paths

SCHEMA = """
CREATE TABLE IF NOT EXISTS job (
    id            INTEGER PRIMARY KEY,
    title         TEXT NOT NULL DEFAULT '',
    created_at    REAL NOT NULL,
    preset_final  TEXT NOT NULL,
    end_mode      TEXT NOT NULL DEFAULT 'exact' CHECK (end_mode IN ('exact', 'guide'))
);

CREATE TABLE IF NOT EXISTS keyframe (
    id            INTEGER PRIMARY KEY,
    job_id        INTEGER NOT NULL REFERENCES job(id) ON DELETE CASCADE,
    position      INTEGER NOT NULL,
    orig_file     TEXT NOT NULL,
    file          TEXT NOT NULL,
    match_applied INTEGER NOT NULL DEFAULT 0,
    match_params  TEXT,
    deviation     TEXT,
    created_at    REAL NOT NULL,
    UNIQUE (job_id, position)
);

CREATE TABLE IF NOT EXISTS segment (
    id               INTEGER PRIMARY KEY,
    job_id           INTEGER NOT NULL REFERENCES job(id) ON DELETE CASCADE,
    idx              INTEGER NOT NULL,
    kf_in            INTEGER NOT NULL,
    kf_out           INTEGER,
    end_mode         TEXT NOT NULL DEFAULT 'exact' CHECK (end_mode IN ('exact', 'guide')),
    prompt           TEXT NOT NULL DEFAULT '',
    duration_s       REAL NOT NULL DEFAULT 2.5,
    retained_take_id INTEGER,
    UNIQUE (job_id, idx)
);

CREATE TABLE IF NOT EXISTS take (
    id               INTEGER PRIMARY KEY,
    segment_id       INTEGER NOT NULL REFERENCES segment(id) ON DELETE CASCADE,
    tier             TEXT NOT NULL CHECK (tier IN ('preview', 'final')),
    preset           TEXT NOT NULL,
    model_type       TEXT NOT NULL,
    memory_profile   REAL NOT NULL,
    settings         TEXT NOT NULL,
    seed             INTEGER NOT NULL,
    fps              INTEGER NOT NULL,
    status           TEXT NOT NULL DEFAULT 'queued'
                     CHECK (status IN ('queued', 'running', 'done', 'failed', 'cancelled')),
    cancel_requested INTEGER NOT NULL DEFAULT 0,
    interruptions    INTEGER NOT NULL DEFAULT 0,
    created_at       REAL NOT NULL,
    started_at       REAL,
    finished_at      REAL,
    phase            TEXT,
    raw_phase        TEXT,
    step             INTEGER,
    total_steps      INTEGER,
    progress         INTEGER,
    phase_started_at REAL,
    output_file      TEXT,
    preview_file     TEXT,
    log_tail         TEXT,
    error            TEXT,
    durations        TEXT
);

CREATE TABLE IF NOT EXISTS assembly (
    id               INTEGER PRIMARY KEY,
    job_id           INTEGER NOT NULL REFERENCES job(id) ON DELETE CASCADE,
    take_ids         TEXT NOT NULL,
    color_match      INTEGER NOT NULL DEFAULT 1,
    fps_mode         TEXT NOT NULL DEFAULT 'native' CHECK (fps_mode IN ('native', 'x2')),
    status           TEXT NOT NULL DEFAULT 'queued'
                     CHECK (status IN ('queued', 'running', 'done', 'failed', 'cancelled')),
    cancel_requested INTEGER NOT NULL DEFAULT 0,
    interruptions    INTEGER NOT NULL DEFAULT 0,
    created_at       REAL NOT NULL,
    started_at       REAL,
    finished_at      REAL,
    phase            TEXT,
    file             TEXT,
    error            TEXT
);

CREATE TABLE IF NOT EXISTS worker (
    id             INTEGER PRIMARY KEY CHECK (id = 1),
    pid            INTEGER,
    started_at     REAL,
    heartbeat      REAL,
    loaded_model   TEXT,
    loaded_profile REAL,
    current_kind   TEXT,
    current_id     INTEGER
);

CREATE INDEX IF NOT EXISTS take_status ON take (status);
CREATE INDEX IF NOT EXISTS take_segment ON take (segment_id);
"""


def now() -> float:
    return time.time()


def connect(path: Path | None = None) -> sqlite3.Connection:
    path = path or paths.db_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path, timeout=10, isolation_level=None, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    conn.execute("PRAGMA busy_timeout=10000")
    conn.executescript(SCHEMA)
    return conn


@contextmanager
def transaction(conn: sqlite3.Connection) -> Iterator[sqlite3.Connection]:
    """BEGIN IMMEDIATE ... COMMIT: takes the write lock up front, so read-then-write is atomic."""
    conn.execute("BEGIN IMMEDIATE")
    try:
        yield conn
    except BaseException:
        conn.execute("ROLLBACK")
        raise
    conn.execute("COMMIT")


def row(r: sqlite3.Row | None) -> dict[str, Any] | None:
    return dict(r) if r is not None else None


def rows(rs: list[sqlite3.Row]) -> list[dict[str, Any]]:
    return [dict(r) for r in rs]


def loads(text: str | None, default: Any = None) -> Any:
    return json.loads(text) if text else default


def update(conn: sqlite3.Connection, table: str, row_id: int, **fields: Any) -> None:
    if not fields:
        return
    cols = ", ".join(f"{k} = ?" for k in fields)
    conn.execute(f"UPDATE {table} SET {cols} WHERE id = ?", (*fields.values(), row_id))

"""SQLite-backed job/event/cost state. One file, zero services."""
from __future__ import annotations

import json
import os
import sqlite3
import threading
import time
import uuid
from typing import Any, Optional

DEFAULT_DB = os.environ.get("HELIX_DB", os.path.expanduser("~/.helix/helix.db"))

SCHEMA = """
CREATE TABLE IF NOT EXISTS jobs (
    id TEXT PRIMARY KEY,
    goal TEXT NOT NULL,
    status TEXT NOT NULL,
    plan_json TEXT,
    result TEXT,
    error TEXT,
    provider TEXT,
    tokens_used INTEGER DEFAULT 0,
    cost_usd REAL DEFAULT 0,
    created_at REAL NOT NULL,
    updated_at REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS events (
    seq INTEGER PRIMARY KEY AUTOINCREMENT,
    job_id TEXT NOT NULL,
    ts REAL NOT NULL,
    type TEXT NOT NULL,
    node_id TEXT,
    data TEXT
);
CREATE INDEX IF NOT EXISTS idx_events_job ON events(job_id, seq);
"""


class Store:
    def __init__(self, path: str = DEFAULT_DB):
        self._path = path
        os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
        self._lock = threading.Lock()
        self._conn = sqlite3.connect(path, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        with self._lock:
            self._conn.executescript(SCHEMA)
            for col in ("workspace_path TEXT", "workspace_branch TEXT"):
                try:
                    self._conn.execute(f"ALTER TABLE jobs ADD COLUMN {col}")
                except sqlite3.OperationalError:
                    pass  # column already exists
            self._conn.commit()

    def close(self):
        self._conn.close()

    def create_job(self, goal: str, provider: str) -> str:
        jid = "job_" + uuid.uuid4().hex[:12]
        now = time.time()
        with self._lock:
            self._conn.execute(
                "INSERT INTO jobs (id, goal, status, provider, created_at, updated_at) VALUES (?,?,?,?,?,?)",
                (jid, goal, "queued", provider, now, now))
            self._conn.commit()
        self.emit(jid, "job_created", None, {"goal": goal})
        return jid

    def update_job(self, jid: str, **fields: Any):
        fields["updated_at"] = time.time()
        cols = ", ".join(f"{k}=?" for k in fields)
        with self._lock:
            self._conn.execute(f"UPDATE jobs SET {cols} WHERE id=?", (*fields.values(), jid))
            self._conn.commit()

    def get_job(self, jid: str) -> Optional[dict]:
        row = self._conn.execute("SELECT * FROM jobs WHERE id=?", (jid,)).fetchone()
        if not row:
            return None
        d = dict(row)
        if d.get("plan_json"):
            d["plan"] = json.loads(d.pop("plan_json"))
        return d

    def list_jobs(self, limit: int = 50) -> list[dict]:
        rows = self._conn.execute(
            "SELECT id, goal, status, tokens_used, cost_usd, created_at, updated_at "
            "FROM jobs ORDER BY created_at DESC LIMIT ?", (limit,)).fetchall()
        return [dict(r) for r in rows]

    def emit(self, jid: str, type_: str, node_id: Optional[str], data: Any):
        with self._lock:
            self._conn.execute(
                "INSERT INTO events (job_id, ts, type, node_id, data) VALUES (?,?,?,?,?)",
                (jid, time.time(), type_, node_id, json.dumps(data) if data is not None else None))
            self._conn.commit()
        hooks_dir = os.path.join(os.path.dirname(self._path), "hooks")
        if os.path.isdir(hooks_dir):
            from .hooks import fire_hooks
            fire_hooks(hooks_dir, type_,
                       {"job_id": jid, "type": type_, "node_id": node_id,
                        "data": data, "ts": time.time()})

    def events_since(self, jid: str, seq: int = 0, limit: int = 500) -> list[dict]:
        rows = self._conn.execute(
            "SELECT seq, ts, type, node_id, data FROM events WHERE job_id=? AND seq>? ORDER BY seq LIMIT ?",
            (jid, seq, limit)).fetchall()
        out = []
        for r in rows:
            d = dict(r)
            if d.get("data"):
                d["data"] = json.loads(d["data"])
            out.append(d)
        return out

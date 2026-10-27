"""Local SQLite journal: audit log, undoable changes, pending approvals.

Everything stays on the developer's machine (default ~/.pgbrake/journal.db).
"""
from __future__ import annotations

import hashlib
import json
import os
import secrets
import sqlite3
import time
from pathlib import Path

FREE_UNDO_HISTORY = 20          # free tier keeps the last N undoable changes
APPROVAL_TTL_SECONDS = 15 * 60

SCHEMA = """
CREATE TABLE IF NOT EXISTS audit (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  ts REAL NOT NULL, client TEXT, db TEXT, sql TEXT NOT NULL, kind TEXT,
  decision TEXT NOT NULL, rowcount INTEGER, change_ids TEXT, pending_id TEXT, message TEXT
);
CREATE TABLE IF NOT EXISTS changes (
  id TEXT PRIMARY KEY, ts REAL NOT NULL, db TEXT, tbl TEXT NOT NULL, op TEXT NOT NULL,
  pk_cols TEXT NOT NULL, before TEXT, after TEXT, sql TEXT, undone_at REAL
);
CREATE TABLE IF NOT EXISTS pending (
  id TEXT PRIMARY KEY, ts REAL NOT NULL, db TEXT, sql TEXT NOT NULL, sql_hash TEXT NOT NULL,
  reasons TEXT, status TEXT NOT NULL DEFAULT 'pending', decided_at REAL, expires REAL NOT NULL
);
"""


def default_path() -> Path:
    return Path(os.environ.get("PGBRAKE_JOURNAL", Path.home() / ".pgbrake" / "journal.db"))


def sql_hash(sql: str) -> str:
    return hashlib.sha256(sql.strip().encode()).hexdigest()


def new_id(prefix: str) -> str:
    return f"{prefix}_{secrets.token_hex(4)}"


class Journal:
    def __init__(self, path: str | Path | None = None, undo_history: int | None = FREE_UNDO_HISTORY):
        self.path = Path(path) if path else default_path()
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.undo_history = undo_history
        self.db = sqlite3.connect(self.path, isolation_level=None, check_same_thread=False)
        self.db.row_factory = sqlite3.Row
        self.db.executescript(SCHEMA)

    # ---- audit -------------------------------------------------------
    def log(self, *, sql, decision, kind=None, rowcount=None, change_ids=None,
            pending_id=None, message=None, client=None, db=None):
        self.db.execute(
            "INSERT INTO audit(ts,client,db,sql,kind,decision,rowcount,change_ids,pending_id,message)"
            " VALUES (?,?,?,?,?,?,?,?,?,?)",
            (time.time(), client, db, sql, kind, decision, rowcount,
             json.dumps(change_ids or []), pending_id, message))

    def audit(self, limit=50):
        return [dict(r) for r in self.db.execute(
            "SELECT * FROM audit ORDER BY id DESC LIMIT ?", (limit,))]

    # ---- changes -----------------------------------------------------
    def add_change(self, *, db, tbl, op, pk_cols, before, after, sql) -> str:
        cid = new_id("chg")
        self.db.execute(
            "INSERT INTO changes(id,ts,db,tbl,op,pk_cols,before,after,sql) VALUES (?,?,?,?,?,?,?,?,?)",
            (cid, time.time(), db, tbl, op, json.dumps(pk_cols),
             json.dumps(before, default=str), json.dumps(after, default=str), sql))
        self._prune()
        return cid

    def _prune(self):
        if self.undo_history is None:
            return
        self.db.execute(
            "DELETE FROM changes WHERE id NOT IN (SELECT id FROM changes ORDER BY ts DESC LIMIT ?)",
            (self.undo_history,))

    def get_change(self, cid):
        r = self.db.execute("SELECT * FROM changes WHERE id=?", (cid,)).fetchone()
        if not r:
            return None
        d = dict(r)
        for k in ("pk_cols", "before", "after"):
            d[k] = json.loads(d[k]) if d[k] else None
        return d

    def changes(self, limit=20):
        return [{k: r[k] for k in ("id", "ts", "tbl", "op", "sql", "undone_at")} |
                {"rows": len(json.loads(r["before"] or "[]") or json.loads(r["after"] or "[]"))}
                for r in self.db.execute("SELECT * FROM changes ORDER BY ts DESC LIMIT ?", (limit,))]

    def mark_undone(self, cid):
        self.db.execute("UPDATE changes SET undone_at=? WHERE id=?", (time.time(), cid))

    # ---- approvals ---------------------------------------------------
    def add_pending(self, *, db, sql, reasons) -> str:
        pid = new_id("apv")
        now = time.time()
        self.db.execute(
            "INSERT INTO pending(id,ts,db,sql,sql_hash,reasons,expires) VALUES (?,?,?,?,?,?,?)",
            (pid, now, db, sql, sql_hash(sql), json.dumps(reasons), now + APPROVAL_TTL_SECONDS))
        return pid

    def get_pending(self, pid):
        r = self.db.execute("SELECT * FROM pending WHERE id=?", (pid,)).fetchone()
        return dict(r) if r else None

    def list_pending(self):
        return [dict(r) for r in self.db.execute(
            "SELECT * FROM pending WHERE status='pending' AND expires>? ORDER BY ts", (time.time(),))]

    def decide(self, pid, status):
        assert status in ("approved", "denied", "used")
        cur = self.db.execute(
            "UPDATE pending SET status=?, decided_at=? WHERE id=? AND status IN ('pending','approved')",
            (status, time.time(), pid))
        return cur.rowcount == 1

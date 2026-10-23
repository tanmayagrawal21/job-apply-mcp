"""Local state for application runs (ADR-0045): runs, per-item outcomes, processed mail and feedback.

Lives in the same `applications.db` as the application store (own tables, own connection, WAL). Stdlib only.
"""
from __future__ import annotations

import asyncio
import json
import sqlite3
import threading
import uuid
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

_SCHEMA = """
CREATE TABLE IF NOT EXISTS intake_runs(
  run_id TEXT PRIMARY KEY,
  started_at TEXT NOT NULL,
  finished_at TEXT,
  params TEXT NOT NULL DEFAULT '{}',
  summary TEXT NOT NULL DEFAULT '{}'
);
CREATE TABLE IF NOT EXISTS intake_items(
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  run_id TEXT NOT NULL,
  ref TEXT NOT NULL,
  url TEXT,
  source TEXT NOT NULL,
  status TEXT NOT NULL,
  why TEXT,
  company TEXT,
  title TEXT,
  approval_id TEXT
);
CREATE INDEX IF NOT EXISTS idx_items_run ON intake_items(run_id);
CREATE TABLE IF NOT EXISTS intake_mail(
  msg_key TEXT PRIMARY KEY,
  run_id TEXT NOT NULL,
  urls INTEGER NOT NULL,
  processed_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS intake_feedback(
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  run_id TEXT,
  ref TEXT,
  kind TEXT NOT NULL,
  payload TEXT NOT NULL,
  created_at TEXT NOT NULL,
  applied_run TEXT
);
"""

CATEGORIES = ("submitted", "dry_run", "confirmed", "handed_off", "unknown", "skipped", "unsupported_site", "failed")


def _now() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")


class IntakeStore:
    def __init__(self, data_dir: str | Path, filename: str = "applications.db"):
        if str(data_dir) == ":memory:":
            target = ":memory:"
        else:
            d = Path(data_dir).expanduser()
            d.mkdir(parents=True, exist_ok=True)
            target = str(d / filename)
        self._db = sqlite3.connect(target, check_same_thread=False, timeout=10)
        self._db.row_factory = sqlite3.Row
        self._lock = threading.Lock()
        with self._lock:
            self._db.execute("PRAGMA journal_mode=WAL")
            self._db.executescript(_SCHEMA)
            self._db.commit()

    def _run(self, fn, *a):
        with self._lock:
            try:
                out = fn(*a)
                self._db.commit()
                return out
            except Exception:
                self._db.rollback()
                raise

    async def _a(self, fn, *a):
        return await asyncio.to_thread(self._run, fn, *a)

    def close(self) -> None:
        with self._lock:
            self._db.close()

    # -- runs ----------------------------------------------------------------------------------------
    async def start_run(self, params: dict[str, Any]) -> str:
        rid = datetime.now(UTC).strftime("%Y%m%d-%H%M%S-") + uuid.uuid4().hex[:6]
        await self._a(lambda: self._db.execute("INSERT INTO intake_runs(run_id, started_at, params) VALUES(?,?,?)",
                                               (rid, _now(), json.dumps(params, default=str))))
        return rid

    async def finish_run(self, run_id: str, summary: dict[str, Any]) -> None:
        await self._a(lambda: self._db.execute("UPDATE intake_runs SET finished_at=?, summary=? WHERE run_id=?",
                                               (_now(), json.dumps(summary, default=str), run_id)))

    async def get_run(self, run_id: str = "") -> dict[str, Any] | None:
        def f():
            q = ("SELECT * FROM intake_runs WHERE run_id=?", (run_id,)) if run_id else \
                ("SELECT * FROM intake_runs ORDER BY started_at DESC, rowid DESC LIMIT 1", ())
            r = self._db.execute(*q).fetchone()
            if r is None:
                return None
            d = dict(r)
            d["params"], d["summary"] = json.loads(d["params"]), json.loads(d["summary"])
            return d
        return await self._a(f)

    async def add_items(self, run_id: str, rows: list[dict[str, Any]]) -> None:
        def f():
            self._db.executemany(
                "INSERT INTO intake_items(run_id, ref, url, source, status, why, company, title, approval_id) "
                "VALUES(?,?,?,?,?,?,?,?,?)",
                [(run_id, r["ref"], r.get("url"), r["source"], r["status"], r.get("why"), r.get("co"),
                  r.get("title"), r.get("approval_id")) for r in rows])
        await self._a(f)

    async def items(self, run_id: str) -> list[dict[str, Any]]:
        return await self._a(lambda: [dict(r) for r in self._db.execute(
            "SELECT * FROM intake_items WHERE run_id=? ORDER BY id", (run_id,))])

    async def set_item_status(self, item_id: int, status: str) -> None:
        await self._a(lambda: self._db.execute("UPDATE intake_items SET status=? WHERE id=?", (status, item_id)))

    async def find_item(self, run_id: str, ref: str) -> dict[str, Any] | None:
        def f():
            r = self._db.execute("SELECT * FROM intake_items WHERE run_id=? AND (ref=? OR url=?) ORDER BY id LIMIT 1",
                                 (run_id, ref, ref)).fetchone()
            return dict(r) if r else None
        return await self._a(f)

    # -- processed mail --------------------------------------------------------------------------------
    async def processed(self, keys: list[str]) -> set[str]:
        def f():
            out: set[str] = set()
            for i in range(0, len(keys), 500):
                chunk = keys[i:i + 500]
                q = f"SELECT msg_key FROM intake_mail WHERE msg_key IN ({','.join('?' * len(chunk))})"
                out |= {r["msg_key"] for r in self._db.execute(q, chunk)}
            return out
        return await self._a(f) if keys else set()

    async def mark_processed(self, run_id: str, done: dict[str, int]) -> None:
        await self._a(lambda: self._db.executemany(
            "INSERT OR IGNORE INTO intake_mail(msg_key, run_id, urls, processed_at) VALUES(?,?,?,?)",
            [(k, run_id, n, _now()) for k, n in done.items()]))

    # -- feedback ------------------------------------------------------------------------------------
    async def add_feedback(self, run_id: str | None, ref: str | None, kind: str, payload: dict[str, Any]) -> int:
        def f():
            cur = self._db.execute(
                "INSERT INTO intake_feedback(run_id, ref, kind, payload, created_at) VALUES(?,?,?,?,?)",
                (run_id, ref, kind, json.dumps(payload, default=str), _now()))
            return int(cur.lastrowid or 0)
        return await self._a(f)

    async def feedback(self, *, pending_only: bool = False) -> list[dict[str, Any]]:
        def f():
            q = "SELECT * FROM intake_feedback" + (" WHERE applied_run IS NULL" if pending_only else "") + " ORDER BY id"
            return [{**dict(r), "payload": json.loads(r["payload"])} for r in self._db.execute(q)]
        return await self._a(f)

    async def mark_feedback_applied(self, ids: list[int], run_id: str) -> None:
        await self._a(lambda: self._db.executemany("UPDATE intake_feedback SET applied_run=? WHERE id=?",
                                                   [(run_id, i) for i in ids]))

"""Account records (ADR-0046): (host, username, created_at, status, terms_url). There is no password column."""
from __future__ import annotations

import sqlite3
import threading
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

STATUSES = ("creating", "active", "pending_verification", "failed", "unknown")
_SCHEMA = """
CREATE TABLE IF NOT EXISTS ats_accounts(
  host TEXT NOT NULL,
  username TEXT NOT NULL,
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL,
  status TEXT NOT NULL,
  terms_url TEXT,
  PRIMARY KEY(host, username)
);
"""


def _now() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")


class AccountsDB:
    def __init__(self, data_dir: str | Path, filename: str = "accounts.db"):
        if str(data_dir) == ":memory:":
            target = ":memory:"
        else:
            d = Path(data_dir).expanduser()
            d.mkdir(parents=True, exist_ok=True)
            target = str(d / filename)
        self._db = sqlite3.connect(target, check_same_thread=False)
        self._db.row_factory = sqlite3.Row
        self._lock = threading.Lock()
        with self._lock:
            self._db.executescript(_SCHEMA)
            self._db.commit()

    def record(self, host: str, username: str, status: str, terms_url: str | None = None) -> None:
        if status not in STATUSES:
            raise ValueError(f"status must be one of {STATUSES}")
        now = _now()
        with self._lock:
            self._db.execute(
                "INSERT INTO ats_accounts(host, username, created_at, updated_at, status, terms_url) "
                "VALUES(?,?,?,?,?,?) ON CONFLICT(host, username) DO UPDATE SET status=excluded.status, "
                "updated_at=excluded.updated_at, terms_url=COALESCE(excluded.terms_url, ats_accounts.terms_url)",
                (host, username, now, now, status, terms_url))
            self._db.commit()

    def get(self, host: str, username: str) -> dict[str, Any] | None:
        with self._lock:
            r = self._db.execute("SELECT * FROM ats_accounts WHERE host=? AND username=?", (host, username)).fetchone()
        return dict(r) if r else None

    def list(self, status: str = "") -> list[dict[str, Any]]:
        q, args = "SELECT * FROM ats_accounts", ()
        if status:
            q, args = q + " WHERE status=?", (status,)
        with self._lock:
            return [dict(r) for r in self._db.execute(q + " ORDER BY host, username", args)]

    def close(self) -> None:
        with self._lock:
            self._db.close()

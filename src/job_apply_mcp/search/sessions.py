"""Search sessions (ADR-0024): one fan-out per search, then free pagination.

A session holds the final filtered+ranked compact rows plus progress counters. The cursor handed to the client is
the opaque string `<session_id>:<offset>`. Sessions live in an in-memory LRU and, optionally, on disk as JSON.
"""
from __future__ import annotations

import json
import os
import re
import secrets
import time
from collections import OrderedDict
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

_SID = re.compile(r"^[A-Za-z0-9]{6,32}$")


class SessionError(Exception):
    """Bad or expired cursor/session; the message tells the caller what to do."""


@dataclass
class Session:
    sid: str
    created: float
    touched: float
    status: str = "running"            # running | done | error
    boards_total: int = 0
    boards_done: int = 0
    boards_failed: int = 0
    started: float = field(default_factory=time.monotonic)
    elapsed_s: float = 0.0
    rows: list[dict[str, Any]] = field(default_factory=list)
    meta: dict[str, Any] = field(default_factory=dict)   # total/extra/filtered/errors

    def progress(self) -> dict[str, Any]:
        el = self.elapsed_s if self.status != "running" else time.monotonic() - self.started
        return {"session": self.sid, "status": self.status, "boards_total": self.boards_total,
                "boards_done": self.boards_done, "boards_failed": self.boards_failed,
                "rows": len(self.rows), "elapsed_s": round(el, 1)}

    def dump(self) -> dict[str, Any]:
        d = dict(self.__dict__)
        d.pop("started", None)
        return d


def parse_cursor(cursor: Any) -> tuple[str | None, int]:
    """None/''/0 -> new search. Int N (legacy) -> offset N of a fresh search. 'sid:N' -> session page."""
    if cursor in (None, "", 0, "0"):
        return None, 0
    if isinstance(cursor, int):
        return None, max(0, cursor)
    sid, sep, off = str(cursor).partition(":")
    if not sep and str(cursor).isdigit():
        return None, int(cursor)
    if not _SID.match(sid) or not off.isdigit():
        raise SessionError("invalid cursor; pass the `next` value from the previous page, or omit cursor to search again")
    return sid, int(off)


class SessionStore:
    def __init__(self, ttl_s: int = 1800, max_sessions: int = 16, directory: Path | None = None, clock=time.time):
        self.ttl_s, self.max, self.dir, self._now = ttl_s, max(1, max_sessions), directory, clock
        self._mem: OrderedDict[str, Session] = OrderedDict()

    def create(self) -> Session:
        now = self._now()
        s = Session(sid=secrets.token_hex(5), created=now, touched=now)
        self._mem[s.sid] = s
        self._evict()
        return s

    def save(self, s: Session) -> None:
        s.touched = self._now()
        self._mem[s.sid] = s
        self._mem.move_to_end(s.sid)
        self._evict()
        if self.dir and s.status != "running":
            try:
                self.dir.mkdir(parents=True, exist_ok=True)
                tmp = self.dir / f".{s.sid}.tmp"
                tmp.write_text(json.dumps(s.dump(), default=str), encoding="utf-8")
                os.replace(tmp, self.dir / f"{s.sid}.json")
            except OSError:
                pass   # persistence is best-effort; the in-memory session still works

    def _evict(self) -> None:
        while len(self._mem) > self.max:
            self._mem.popitem(last=False)

    def get(self, sid: str) -> Session:
        if not _SID.match(sid):
            raise SessionError("invalid session id")
        s = self._mem.get(sid) or self._load(sid)
        if s is None or self._now() - s.touched > self.ttl_s:
            self._mem.pop(sid, None)
            if self.dir:
                (self.dir / f"{sid}.json").unlink(missing_ok=True)
            raise SessionError(f"search session {sid} expired or unknown; rerun search_jobs without a cursor")
        s.touched = self._now()
        self._mem.move_to_end(sid)
        return s

    def _load(self, sid: str) -> Session | None:
        if not self.dir:
            return None
        try:
            d = json.loads((self.dir / f"{sid}.json").read_text(encoding="utf-8"))
            s = Session(**d)
        except (OSError, ValueError, TypeError):
            return None
        self._mem[sid] = s
        self._evict()
        return s

    def prune(self) -> None:
        """Drop expired sessions from memory and disk."""
        now = self._now()
        for sid in [k for k, v in self._mem.items() if now - v.touched > self.ttl_s]:
            del self._mem[sid]
        if self.dir and self.dir.is_dir():
            for f in self.dir.glob("*.json"):
                try:
                    if now - f.stat().st_mtime > self.ttl_s:
                        f.unlink()
                except OSError:
                    pass

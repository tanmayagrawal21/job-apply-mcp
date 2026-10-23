"""Profile gaps and stored answers (ADR-0047). One small SQLite file (`profile.db`) beside the application store.

gap_hits: one row per (question key, application ref): what happened (unresolved | default), whether it blocked the
          application, the default that was used, and the posting it came from.
gaps:     per-key rollup state (open | answered | skipped) and the normalized question text.
answers:  user-given answers keyed by (key, scope, scope_value) with provenance. The newest answer per scope wins.

Ranking is frequency x impact: each hit weighs 3 when it blocked the application (a required field that could not
be filled), 2 when an optional field stayed open, 1 when only a default was used; a key's score is the sum.
"""
from __future__ import annotations

import sqlite3
import threading
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

SCOPES = ("global", "company", "role_family")
_W_BLOCKED, _W_OPEN, _W_DEFAULT = 3, 2, 1
_SCHEMA = """
CREATE TABLE IF NOT EXISTS gaps(
  key TEXT PRIMARY KEY, question TEXT NOT NULL, status TEXT NOT NULL DEFAULT 'open',
  first_seen TEXT NOT NULL, last_seen TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS gap_hits(
  key TEXT NOT NULL, ref TEXT NOT NULL, kind TEXT NOT NULL, blocked INTEGER NOT NULL DEFAULT 0,
  weight INTEGER NOT NULL, default_used TEXT NOT NULL DEFAULT '', company TEXT NOT NULL DEFAULT '',
  title TEXT NOT NULL DEFAULT '', ts TEXT NOT NULL, PRIMARY KEY(key, ref));
CREATE INDEX IF NOT EXISTS idx_hits_key ON gap_hits(key);
CREATE TABLE IF NOT EXISTS assumed_hits(
  kind TEXT NOT NULL, ref TEXT NOT NULL, rule TEXT NOT NULL DEFAULT '', ts TEXT NOT NULL, PRIMARY KEY(kind, ref));
CREATE TABLE IF NOT EXISTS answers(
  key TEXT NOT NULL, scope TEXT NOT NULL, scope_value TEXT NOT NULL DEFAULT '', value TEXT NOT NULL,
  origin TEXT NOT NULL DEFAULT 'user_written', ts TEXT NOT NULL, PRIMARY KEY(key, scope, scope_value));
"""


def _now() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")


def _norm_scope(scope: str, value: str) -> str:
    if scope == "company":
        from ..store.sqlite import company_key
        return company_key(value)
    return value.strip().lower() if scope == "role_family" else ""


class ProfileDB:
    def __init__(self, data_dir: str | Path, filename: str = "profile.db"):
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

    def close(self) -> None:
        with self._lock:
            self._db.close()

    # -- gaps ----------------------------------------------------------------------------------
    def record_hit(self, key: str, question: str, *, ref: str, kind: str, blocked: bool = False,
                   default_used: str = "", company: str = "", title: str = "") -> None:
        """Idempotent per (key, ref): re-planning the same posting updates its row instead of double counting."""
        weight = _W_BLOCKED if blocked else (_W_OPEN if kind == "unresolved" else _W_DEFAULT)
        now = _now()
        with self._lock:
            self._db.execute(
                "INSERT INTO gaps(key,question,first_seen,last_seen) VALUES(?,?,?,?) "
                "ON CONFLICT(key) DO UPDATE SET last_seen=excluded.last_seen", (key, question[:300], now, now))
            self._db.execute(
                "INSERT INTO gap_hits(key,ref,kind,blocked,weight,default_used,company,title,ts) VALUES(?,?,?,?,?,?,?,?,?) "
                "ON CONFLICT(key,ref) DO UPDATE SET kind=excluded.kind, blocked=excluded.blocked, weight=excluded.weight, "
                "default_used=excluded.default_used, ts=excluded.ts",
                (key, ref, kind, int(blocked), weight, default_used[:200], company[:80], title[:120], now))
            self._db.commit()

    def clear_ref(self, ref: str, keep: set[str]) -> None:
        """A re-plan that now resolves a key removes that ref's old hit for it."""
        with self._lock:
            rows = self._db.execute("SELECT key FROM gap_hits WHERE ref=?", (ref,)).fetchall()
            for r in rows:
                if r["key"] not in keep:
                    self._db.execute("DELETE FROM gap_hits WHERE key=? AND ref=?", (r["key"], ref))
            self._db.commit()

    def questions(self, limit: int = 5, examples: int = 3, include_skipped: bool = False) -> list[dict[str, Any]]:
        """Open gaps, highest frequency x impact first. Compact rows; `n` = distinct applications."""
        st = "('open','skipped')" if include_skipped else "('open')"
        with self._lock:
            rows = self._db.execute(
                f"SELECT g.key, g.question, COUNT(h.ref) n, COALESCE(SUM(h.weight),0) score, "
                f"COALESCE(SUM(h.blocked),0) blocked FROM gaps g JOIN gap_hits h ON h.key=g.key "
                f"WHERE g.status IN {st} GROUP BY g.key ORDER BY score DESC, n DESC, g.key LIMIT ?",
                (max(1, limit),)).fetchall()
            out = []
            for r in rows:
                ex = self._db.execute(
                    "SELECT company,title,ref,default_used FROM gap_hits WHERE key=? ORDER BY weight DESC, ts DESC LIMIT ?",
                    (r["key"], examples)).fetchall()
                d = next((e["default_used"] for e in ex if e["default_used"]), "")
                row = {"key": r["key"], "question": r["question"], "n": r["n"], "blocked": r["blocked"],
                       "score": r["score"], "examples": [f"{e['company']} - {e['title']}".strip(" -") or e["ref"] for e in ex]}
                if d:
                    row["default_used"] = d
                out.append(row)
            return out

    def gap(self, key: str) -> dict[str, Any] | None:
        with self._lock:
            r = self._db.execute("SELECT * FROM gaps WHERE key=?", (key,)).fetchone()
            return dict(r) if r else None

    def set_status(self, key: str, status: str) -> None:
        with self._lock:
            self._db.execute("UPDATE gaps SET status=? WHERE key=?", (status, key))
            self._db.commit()

    # -- assumed answers (ADR-0052) -----------------------------------------------------------------
    def record_assumed(self, kind: str, ref: str, rule: str = "") -> int:
        """Count one assumption of a question kind for an application (idempotent per ref); returns the kind's total
        across runs (distinct applications)."""
        with self._lock:
            self._db.execute("INSERT INTO assumed_hits(kind,ref,rule,ts) VALUES(?,?,?,?) "
                             "ON CONFLICT(kind,ref) DO UPDATE SET rule=excluded.rule, ts=excluded.ts",
                             (kind, ref, rule[:120], _now()))
            self._db.commit()
            return int(self._db.execute("SELECT COUNT(*) FROM assumed_hits WHERE kind=?", (kind,)).fetchone()[0])

    def assumed_count(self, kind: str) -> int:
        with self._lock:
            return int(self._db.execute("SELECT COUNT(*) FROM assumed_hits WHERE kind=?", (kind,)).fetchone()[0])

    # -- answers -------------------------------------------------------------------------------
    def set_answer(self, key: str, value: str, scope: str = "global", scope_value: str = "",
                   origin: str = "user_written") -> None:
        """Only human paths (elicitation, TTY CLI) call this; the origin is always user_written."""
        if origin != "user_written":
            raise ValueError("only answers given by the user are stored")
        if scope not in SCOPES:
            raise ValueError(f"scope must be one of {SCOPES}")
        if scope != "global" and not scope_value:
            raise ValueError(f"scope={scope} needs a scope value (company name or role family)")
        scope_value = _norm_scope(scope, scope_value)
        with self._lock:
            self._db.execute(
                "INSERT INTO answers(key,scope,scope_value,value,origin,ts) VALUES(?,?,?,?,?,?) "
                "ON CONFLICT(key,scope,scope_value) DO UPDATE SET value=excluded.value, origin=excluded.origin, ts=excluded.ts",
                (key, scope, scope_value, value, origin, _now()))
            self._db.execute("UPDATE gaps SET status='answered' WHERE key=?", (key,))
            self._db.commit()

    def answer(self, key: str, *, company: str = "", family: str = "") -> str | None:
        r = self.answer_row(key, company=company, family=family)
        return r[0] if r else None

    def answer_row(self, key: str, *, company: str = "", family: str = "") -> tuple[str, str] | None:
        """(value, origin) from the most specific scope: company, then role family, then global."""
        with self._lock:
            for scope, sv in (("company", _norm_scope("company", company)),
                              ("role_family", _norm_scope("role_family", family)), ("global", "")):
                if scope != "global" and not sv:
                    continue
                r = self._db.execute("SELECT value,origin FROM answers WHERE key=? AND scope=? AND scope_value=?",
                                     (key, scope, sv)).fetchone()
                if r:
                    return str(r["value"]), str(r["origin"])
        return None

    def all_answers(self) -> list[dict[str, Any]]:
        with self._lock:
            return [dict(r) for r in self._db.execute("SELECT key,scope,scope_value,value,origin,ts FROM answers")]


_SHARED: dict[str, ProfileDB] = {}


def shared(data_dir: str | Path) -> ProfileDB:
    """One connection per data dir per process, shared by the engine, the MCP tools and the CLI."""
    k = str(Path(data_dir).expanduser()) if str(data_dir) != ":memory:" else ":memory:"
    if k == ":memory:":
        return ProfileDB(k)
    if k not in _SHARED:
        _SHARED[k] = ProfileDB(k)
    return _SHARED[k]

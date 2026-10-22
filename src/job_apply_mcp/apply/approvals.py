"""Server-held approvals, submission state machine and audit log (ADR-0021, amends ADR-0011).

Approvals are opaque random ids issued by the server and stored in `<data_dir>/approvals.db`; a client can never
compute one. Each record binds ref + destination URL + a hash of the final answers + a hash of the files. Any change
invalidates the record (it falls back to `pending`). In `approval_mode="human"` the record must additionally be
marked by a human channel (MCP elicitation or the `job-apply-mcp approve <id>` CLI) before submit proceeds.

Submission state per ref: planned -> approved -> submitting -> submitted | failed | unknown | handoff.
`submitting` is persisted BEFORE the click. A ref left in submitting/unknown is never auto-resubmitted; only
`resolve_submission` moves it on.
"""
from __future__ import annotations

import hashlib
import json
import secrets
import sqlite3
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

STATES = ("planned", "approved", "submitting", "submitted", "failed", "unknown", "handoff")
BLOCKING = ("submitting", "unknown", "submitted")        # begin_submit refuses these
RESOLVABLE = ("submitting", "unknown")
OUTCOMES = ("submitted", "failed", "handoff")

_SCHEMA = """
CREATE TABLE IF NOT EXISTS approvals(
  approval_id TEXT PRIMARY KEY, ref TEXT NOT NULL, url TEXT NOT NULL, company TEXT NOT NULL DEFAULT '',
  title TEXT NOT NULL DEFAULT '', answers_hash TEXT NOT NULL, files_hash TEXT NOT NULL, summary TEXT NOT NULL,
  status TEXT NOT NULL, approved_by TEXT, created REAL NOT NULL, expires REAL NOT NULL);
CREATE INDEX IF NOT EXISTS idx_appr_ref ON approvals(ref);
CREATE TABLE IF NOT EXISTS submissions(
  ref TEXT PRIMARY KEY, state TEXT NOT NULL, idem_key TEXT NOT NULL, approval_id TEXT, company TEXT NOT NULL DEFAULT '',
  title TEXT NOT NULL DEFAULT '', detail TEXT, evidence TEXT, updated REAL NOT NULL);
CREATE TABLE IF NOT EXISTS approval_audit(
  id INTEGER PRIMARY KEY AUTOINCREMENT, ts REAL NOT NULL, ref TEXT, approval_id TEXT, event TEXT NOT NULL,
  actor TEXT, detail TEXT);
"""


def answers_digest(answers: dict[str, Any]) -> str:
    blob = json.dumps(answers, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(blob.encode()).hexdigest()


def files_digest(files: dict[str, str]) -> str:
    """Hash of file contents (path when unreadable), so swapping a resume under the same path invalidates."""
    parts = {}
    for k, p in sorted(files.items()):
        try:
            parts[k] = hashlib.sha256(Path(p).expanduser().read_bytes()).hexdigest()
        except OSError:
            parts[k] = "path:" + str(p)
    return answers_digest(parts)


def idempotency_key(ref: str, url: str) -> str:
    return hashlib.sha256(f"{ref}\n{url}".encode()).hexdigest()[:32]


def summarize(company: str, title: str, answers: dict[str, Any], limit: int = 20, width: int = 100) -> dict[str, Any]:
    rows = {k: (str(v) if len(str(v)) <= width else str(v)[:width] + "...") for k, v in list(answers.items())[:limit]}
    return {"company": company, "title": title, "n_answers": len(answers), "answers": rows}


@dataclass
class Approval:
    approval_id: str
    ref: str
    url: str
    company: str
    title: str
    answers_hash: str
    files_hash: str
    summary: dict[str, Any]
    status: str                 # pending | approved | denied | used
    approved_by: str | None
    created: float
    expires: float


class ApprovalBook:
    def __init__(self, data_dir: str | Path = ":memory:", *, ttl_s: int = 1800, clock=time.time):
        if str(data_dir) == ":memory:":
            target = ":memory:"
        else:
            d = Path(data_dir).expanduser()
            d.mkdir(parents=True, exist_ok=True)
            target = str(d / "approvals.db")
        self.ttl_s, self._clock = ttl_s, clock
        self._lock = threading.RLock()
        self._db = sqlite3.connect(target, check_same_thread=False, timeout=10)
        self._db.row_factory = sqlite3.Row
        with self._lock:
            self._db.execute("PRAGMA journal_mode=WAL")
            self._db.executescript(_SCHEMA)
            self._db.commit()

    def _x(self, sql: str, args: tuple = ()) -> sqlite3.Cursor:
        with self._lock:
            cur = self._db.execute(sql, args)
            self._db.commit()
            return cur

    def audit(self, event: str, *, ref: str | None = None, approval_id: str | None = None,
              actor: str = "server", detail: str = "") -> None:
        self._x("INSERT INTO approval_audit(ts,ref,approval_id,event,actor,detail) VALUES(?,?,?,?,?,?)",
                (self._clock(), ref, approval_id, event, actor, detail[:300]))

    def audit_rows(self, ref: str | None = None) -> list[dict[str, Any]]:
        q, a = ("SELECT * FROM approval_audit WHERE ref=? ORDER BY id", (ref,)) if ref else \
               ("SELECT * FROM approval_audit ORDER BY id", ())
        with self._lock:
            return [dict(r) for r in self._db.execute(q, a).fetchall()]

    # ------------------------------------------------------------------ approvals
    def issue(self, ref: str, url: str, company: str, title: str, answers: dict[str, Any],
              files: dict[str, str] | None = None) -> str:
        aid = secrets.token_urlsafe(24)
        now = self._clock()
        self._x("INSERT INTO approvals VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
                (aid, ref, url, company, title, answers_digest(answers), files_digest(files or {}),
                 json.dumps(summarize(company, title, answers)), "pending", None, now, now + self.ttl_s))
        self.audit("issued", ref=ref, approval_id=aid)
        return aid

    def _row(self, r: sqlite3.Row | None) -> Approval | None:
        if r is None:
            return None
        d = dict(r)
        d["summary"] = json.loads(d["summary"])
        return Approval(**d)

    def get(self, approval_id: str | None) -> Approval | None:
        if not approval_id:
            return None
        with self._lock:
            return self._row(self._db.execute("SELECT * FROM approvals WHERE approval_id=?",
                                              (approval_id,)).fetchone())

    def pending(self) -> list[Approval]:
        with self._lock:
            rows = self._db.execute("SELECT * FROM approvals WHERE status='pending' AND expires>? ORDER BY created",
                                    (self._clock(),)).fetchall()
        return [a for a in (self._row(r) for r in rows) if a]

    def check(self, approval_id: str | None, ref: str, url: str) -> tuple[Approval | None, str]:
        """Existence/ownership/expiry/denied/used. Hash and human approval are checked separately."""
        a = self.get(approval_id)
        if a is None:
            return None, "unknown approval_id; call plan_application for a server-issued one"
        if a.ref != ref or a.url != url:
            return None, "approval_id was issued for a different ref/destination"
        if a.expires < self._clock():
            return None, "approval_id expired; re-plan"
        if a.status == "denied":
            return None, "approval was denied"
        if a.status == "used":
            return None, "approval_id already used; re-plan"
        return a, ""

    def rebind(self, a: Approval, company: str, title: str, answers: dict[str, Any],
               files: dict[str, str] | None = None) -> Approval:
        """Final answers differ from what was bound: approval is reset to pending (any change invalidates)."""
        self._x("UPDATE approvals SET answers_hash=?, files_hash=?, summary=?, status='pending', approved_by=? "
                "WHERE approval_id=?", (answers_digest(answers), files_digest(files or {}),
                                        json.dumps(summarize(company, title, answers)), None, a.approval_id))
        self.audit("rebound", ref=a.ref, approval_id=a.approval_id, detail="answers changed; approval reset")
        return self.get(a.approval_id)  # type: ignore[return-value]

    def matches(self, a: Approval, answers: dict[str, Any], files: dict[str, str] | None = None) -> bool:
        return (secrets.compare_digest(a.answers_hash, answers_digest(answers))
                and secrets.compare_digest(a.files_hash, files_digest(files or {})))

    def approve(self, approval_id: str, by: str) -> tuple[bool, str]:
        a = self.get(approval_id)
        if a is None:
            return False, "unknown approval_id"
        if a.expires < self._clock():
            return False, "expired"
        if a.status in ("denied", "used"):
            return False, f"already {a.status}"
        self._x("UPDATE approvals SET status='approved', approved_by=? WHERE approval_id=?", (by, approval_id))
        self.audit("approved", ref=a.ref, approval_id=approval_id, actor=by)
        self.set_state(a.ref, "approved", company=a.company, title=a.title, approval_id=approval_id,
                       url=a.url, only_from=("planned", "approved", "failed", "handoff"))
        return True, "ok"

    def deny(self, approval_id: str, by: str) -> tuple[bool, str]:
        a = self.get(approval_id)
        if a is None:
            return False, "unknown approval_id"
        self._x("UPDATE approvals SET status='denied', approved_by=? WHERE approval_id=?", (by, approval_id))
        self.audit("denied", ref=a.ref, approval_id=approval_id, actor=by)
        return True, "ok"

    # ------------------------------------------------------------------ submission state
    def state(self, ref: str) -> dict[str, Any] | None:
        with self._lock:
            r = self._db.execute("SELECT * FROM submissions WHERE ref=?", (ref,)).fetchone()
        return dict(r) if r else None

    def set_state(self, ref: str, state: str, *, company: str = "", title: str = "", approval_id: str | None = None,
                  url: str = "", detail: str | None = None, evidence: str | None = None,
                  only_from: tuple[str, ...] | None = None) -> bool:
        """Upsert. `only_from` restricts which existing states may be overwritten (never downgrades blocking ones)."""
        assert state in STATES
        with self._lock:
            cur = self.state(ref)
            if cur and only_from is not None and cur["state"] not in only_from:
                return False
            if cur is None:
                self._db.execute("INSERT INTO submissions VALUES(?,?,?,?,?,?,?,?,?)",
                                 (ref, state, idempotency_key(ref, url), approval_id, company, title, detail,
                                  evidence, self._clock()))
            else:
                self._db.execute(
                    "UPDATE submissions SET state=?, approval_id=COALESCE(?,approval_id), company=COALESCE(NULLIF(?,''),company),"
                    " title=COALESCE(NULLIF(?,''),title), detail=COALESCE(?,detail), evidence=COALESCE(?,evidence), updated=? "
                    "WHERE ref=?", (state, approval_id, company, title, detail, evidence, self._clock(), ref))
            self._db.commit()
        self.audit(f"state:{state}", ref=ref, approval_id=approval_id, detail=detail or "")
        return True

    def begin_submit(self, ref: str, approval_id: str | None) -> tuple[bool, str]:
        """Atomically move to `submitting` (persisted BEFORE the click) and consume the approval."""
        with self._lock:
            cur = self.state(ref)
            if cur and cur["state"] in BLOCKING:
                return False, cur["state"]
            ok = self.set_state(ref, "submitting", approval_id=approval_id, detail="click about to happen")
            if approval_id:
                self._x("UPDATE approvals SET status='used' WHERE approval_id=?", (approval_id,))
        return ok, "submitting"

    def finish(self, ref: str, state: str, detail: str = "", evidence: str | None = None) -> None:
        self.set_state(ref, state, detail=detail[:300], evidence=evidence)

    def resolve(self, ref: str, outcome: str, note: str, actor: str) -> tuple[bool, str]:
        cur = self.state(ref)
        if cur is None:
            return False, "no submission record for ref"
        if cur["state"] not in RESOLVABLE:
            return False, f"state is '{cur['state']}'; only submitting/unknown can be resolved"
        if outcome not in OUTCOMES:
            return False, f"outcome must be one of {list(OUTCOMES)}"
        self.set_state(ref, outcome, detail=f"resolved by {actor}: {note}"[:300])
        self.audit("resolved", ref=ref, actor=actor, detail=f"{outcome}: {note}")
        return True, outcome


def evidence_path(data_dir: str | Path, ref: str) -> str | None:
    d = Path(data_dir).expanduser() / "evidence" / hashlib.sha256(ref.encode()).hexdigest()[:12]
    return str(d) if d.exists() else None


def cli(argv: list[str], data_dir: str | Path, ttl_s: int = 1800) -> int:
    """`job-apply-mcp approve <id> | deny <id> | pending | resolve <ref> <submitted|failed|handoff> [note]`."""
    book = ApprovalBook(data_dir, ttl_s=ttl_s)
    cmd, rest = argv[0], argv[1:]
    if cmd == "pending":
        for a in book.pending():
            print(f"{a.approval_id}  {a.summary['company']} | {a.summary['title']}  ({a.summary['n_answers']} answers)")
        return 0
    if cmd in ("approve", "deny") and len(rest) == 1:
        a = book.get(rest[0])
        if cmd == "approve" and a:
            print(json.dumps(a.summary, indent=2))
        ok, msg = (book.approve if cmd == "approve" else book.deny)(rest[0], "human:cli")
        print(f"{cmd}: {msg}")
        return 0 if ok else 1
    if cmd == "resolve" and len(rest) >= 2:
        ok, msg = book.resolve(rest[0], rest[1], " ".join(rest[2:]), "human:cli")
        print(msg)
        return 0 if ok else 1
    print("usage: job-apply-mcp approve <approval_id> | deny <approval_id> | pending | "
          "resolve <ref> <submitted|failed|handoff> [note]")
    return 2

"""SQLite application store and duplicate index (ADR-0010).

Stdlib only. One connection in WAL mode guarded by a thread lock; calls run in a worker thread so the
event loop never blocks. Duplicate detection uses normalized company keys and fuzzy title matching.
`company_lock()` gives the apply engine a per-company asyncio lock for the check-then-submit sequence
(ADR-0002 idempotency):

    async with store.company_lock(company):
        if await store.seen(company, title): ...skip...
        result = await submit(...)
        await store.record(result, company=company, title=title)
"""
from __future__ import annotations

import asyncio
import csv
import re
import sqlite3
import threading
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any
from urllib.parse import parse_qsl, urlencode, urlsplit

from ..core.models import SubmitResult

CSV_COLUMNS = ["ref", "company", "title", "status", "via", "confirmation", "created_at"]
# Statuses that mean "an application really went out" and therefore block re-applying.
SEEN_STATUSES = ("submitted", "duplicate")

_COMPANY_SUFFIX = {"inc", "incorporated", "llc", "ltd", "limited", "corp", "corporation", "co", "company",
                   "gmbh", "plc", "the", "lp", "llp", "global", "technologies", "technology", "tech",
                   "group", "holdings", "international", "worldwide", "sa", "ag", "bv", "pty"}
# Placeholder ref prefixes of imported / mail-derived rows: these are NOT platform requisition ids.
_SYNTHETIC_REF = ("mail:", "email:", "import:", "imported:", "csv:", "manual:", "history:", "legacy:")
_TITLE_STOP = {"the", "a", "an", "and", "of", "for", "at", "remote", "hybrid", "onsite", "us", "usa"}
# Level words must match exactly between two titles for them to be considered the same job.
_LEVEL_ALIASES = {"sr": "senior", "jr": "junior", "intern": "internship", "interns": "internship",
                  "1": "i", "2": "ii", "3": "iii", "4": "iv"}
_LEVELS = {"i", "ii", "iii", "iv", "v", "senior", "junior", "staff", "principal", "lead", "internship",
           "associate", "director", "manager", "head", "vp", "entry", "new", "grad"}

_SCHEMA = """
CREATE TABLE IF NOT EXISTS applications(
  ref TEXT PRIMARY KEY,
  company TEXT NOT NULL DEFAULT '',
  title TEXT NOT NULL DEFAULT '',
  company_key TEXT NOT NULL,
  title_key TEXT NOT NULL,
  status TEXT NOT NULL,
  via TEXT,
  confirmation TEXT,
  created_at TEXT NOT NULL,
  req_id TEXT,
  url_key TEXT
);
CREATE INDEX IF NOT EXISTS idx_app_company ON applications(company_key);
CREATE INDEX IF NOT EXISTS idx_app_status ON applications(status, created_at);
CREATE TABLE IF NOT EXISTS events(
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  ref TEXT NOT NULL,
  ts TEXT NOT NULL,
  kind TEXT NOT NULL,
  detail TEXT
);
CREATE INDEX IF NOT EXISTS idx_events_ref ON events(ref);
"""


def company_key(name: str) -> str:
    toks = [t for t in re.sub(r"[^a-z0-9 ]+", " ", name.lower().replace("&", " and ")).split()]
    while toks and toks[-1] in _COMPANY_SUFFIX:
        toks.pop()
    toks = [t for t in toks if t != "the"] or toks
    return " ".join(toks)


def company_resolver(aliases: dict[str, str] | None = None):
    """Return f(name)->identity. Identity = normalized key (suffixes stripped, board slugs like 'acme-games' or
    'acmegames' treated as the display name 'Acme Games' by ignoring separators) mapped through the user's alias
    map {"brand": "canonical"} (both sides normalized the same way)."""
    amap = {company_key(a).replace(" ", ""): company_key(c).replace(" ", "")
            for a, c in (aliases or {}).items() if a and c}

    def resolve(name: str) -> str:
        k = company_key(name).replace(" ", "")
        for _ in range(4):                      # follow short alias chains, bounded against cycles
            nk = amap.get(k)
            if not nk or nk == k:
                break
            k = nk
        return k
    return resolve


def title_similarity(a: str, b: str) -> float:
    """Token Jaccard in [0,1]; 0 when seniority/level tokens disagree or either title is empty."""
    ta, tb = set(title_tokens(a)), set(title_tokens(b))
    if not ta or not tb or (ta & _LEVELS) != (tb & _LEVELS):
        return 0.0
    return len(ta & tb) / len(ta | tb)


def title_tokens(title: str) -> list[str]:
    t = re.sub(r"\([^)]*\)|\[[^]]*\]", " ", title.lower())          # drop parentheticals
    t = re.sub(r"\b[a-z]{0,3}-?\d{4,}\b", " ", t)                   # requisition ids like R-12345
    t = re.sub(r"[^a-z0-9 ]+", " ", t.replace("&", " and "))
    out = [_LEVEL_ALIASES.get(w, w) for w in t.split() if w not in _TITLE_STOP]
    return out


def title_key(title: str) -> str:
    return " ".join(sorted(title_tokens(title)))


def titles_similar(a: str, b: str, threshold: float = 0.6) -> bool:
    """Order-insensitive token Jaccard. Seniority/level tokens must agree exactly."""
    sim = title_similarity(a, b)
    return sim > 0 and sim >= threshold


_TRACKING = {"gclid", "fbclid", "msclkid", "mc_cid", "mc_eid", "ref", "referrer", "referer", "source", "src",
             "trk", "trkinfo", "lever-source", "lever-origin", "gh_src", "icid", "cid", "campaign", "from",
             "vs", "sid", "_hsenc", "_hsmi", "li_fat_id", "refid", "trackingid", "jobboard", "mkt_tok"}
_TRACKING_PREFIX = ("utm_", "ga_", "pk_", "mtm_")


def canonical_url(url: str | None) -> str | None:
    """Stable identity for an application URL: lowercase host (no www), no fragment or trailing slash, tracking
    params dropped, remaining params sorted. Job-identifying params (gh_jid, jid, id, ...) are kept."""
    if not url or not url.strip().lower().startswith(("http://", "https://")):
        return None
    u = urlsplit(url.strip())
    host = (u.hostname or "").lower().removeprefix("www.")
    if not host:
        return None
    q = sorted((k.lower(), v) for k, v in parse_qsl(u.query, keep_blank_values=False)
               if k.lower() not in _TRACKING and not k.lower().startswith(_TRACKING_PREFIX))
    path = re.sub(r"/+", "/", u.path).rstrip("/")
    return f"{host}{path}" + (f"?{urlencode(q)}" if q else "")


def identity_key(ref: str | None) -> str | None:
    """'<platform>:<board>:<job_id>' refs are platform requisition ids; anything else has no stable id."""
    if (not ref or ref.lower().startswith(("http:", "https:") + _SYNTHETIC_REF)
            or len(ref.split(":", 2)) < 3):
        return None
    return ref.strip().lower()


def _now() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")


class SqliteStore:
    def __init__(self, data_dir: str | Path, *, dedupe_days: int = 180, dedupe_mode: str = "id_first",
                 company_aliases: dict[str, str] | None = None, title_threshold: float = 0.6,
                 filename: str = "applications.db"):
        if str(data_dir) == ":memory:":
            self.path, target = Path(":memory:"), ":memory:"
        else:
            d = Path(data_dir).expanduser()
            d.mkdir(parents=True, exist_ok=True)
            self.path = d / filename
            target = str(self.path)
        self.dedupe_days, self.dedupe_mode = dedupe_days, dedupe_mode
        self.title_threshold = title_threshold
        self._resolve = company_resolver(company_aliases)
        self._db = sqlite3.connect(target, check_same_thread=False)
        self._db.row_factory = sqlite3.Row
        self._tlock = threading.Lock()
        self._locks: dict[str, asyncio.Lock] = {}
        with self._tlock:
            self._db.execute("PRAGMA journal_mode=WAL")
            self._db.execute("PRAGMA synchronous=NORMAL")
            self._db.executescript(_SCHEMA)
            self._migrate()
            self._db.commit()

    def _migrate(self) -> None:
        """Add the nullable identity columns to databases created before ADR-0010's amendment; idempotent."""
        cols = {r["name"] for r in self._db.execute("PRAGMA table_info(applications)")}
        for c in ("req_id", "url_key"):
            if c not in cols:
                self._db.execute(f"ALTER TABLE applications ADD COLUMN {c} TEXT")
        self._db.execute("CREATE INDEX IF NOT EXISTS idx_app_req ON applications(req_id)")
        self._db.execute("CREATE INDEX IF NOT EXISTS idx_app_url ON applications(url_key)")
        for r in self._db.execute("SELECT ref FROM applications WHERE req_id IS NULL").fetchall():
            if k := identity_key(r["ref"]):
                self._db.execute("UPDATE applications SET req_id=? WHERE ref=?", (k, r["ref"]))

    # -- plumbing -----------------------------------------------------------------------------
    def _run(self, fn, *args):
        with self._tlock:
            try:
                out = fn(*args)
                self._db.commit()
                return out
            except Exception:
                self._db.rollback()
                raise

    async def _a(self, fn, *args):
        return await asyncio.to_thread(self._run, fn, *args)

    def close(self) -> None:
        with self._tlock:
            self._db.close()

    def company_lock(self, company: str) -> asyncio.Lock:
        """Per-company lock for the apply engine's check-then-submit-then-record sequence."""
        return self._locks.setdefault(company_key(company), asyncio.Lock())

    # -- Store protocol -----------------------------------------------------------------------
    async def seen(self, company: str, title: str, *, within_days: int | None = None, ref: str | None = None,
                   url: str | None = None, mode: str | None = None) -> bool:
        return await self.seen_why(company, title, within_days=within_days, ref=ref, url=url, mode=mode) is not None

    async def seen_why(self, company: str, title: str, *, within_days: int | None = None, ref: str | None = None,
                       url: str | None = None, mode: str | None = None) -> str | None:
        """Matched reason or None: 'req_id:<id>', 'url:<canonical>', or 'title~<jaccard> company=<rule>' where rule
        is 'exact' (same normalized key), 'alias' (dedupe.company_aliases / separator-insensitive slug match) and
        'legacy-row' is appended when the stored row had no id. In id_first mode a known requisition id / URL that
        matches is authoritative; company+title is ALSO tried against stored rows that lack an id and url_key
        (imported/legacy history). Only rows with a different id count as distinct requisitions."""
        days = self.dedupe_days if within_days is None else within_days
        return await self._a(self._seen, company, title, days, ref, url, mode or self.dedupe_mode)

    def _seen(self, company: str, title: str, days: int, ref: str | None, url: str | None, mode: str) -> str | None:
        if days <= 0:
            return None
        cutoff = (datetime.now(UTC) - timedelta(days=days)).isoformat(timespec="seconds")
        marks = ",".join("?" * len(SEEN_STATUSES))
        base = f"FROM applications WHERE created_at>=? AND status IN ({marks})"
        args = (cutoff, *SEEN_STATUSES)
        rid, uk = identity_key(ref), canonical_url(url)
        title_only = mode == "title" or not (rid or uk)
        if not title_only:
            if rid and self._db.execute(f"SELECT 1 {base} AND req_id=?", (*args, rid)).fetchone():
                return f"req_id:{rid}"
            if uk and self._db.execute(f"SELECT 1 {base} AND url_key=?", (*args, uk)).fetchone():
                return f"url:{uk}"
        extra = "" if title_only else " AND req_id IS NULL AND url_key IS NULL"
        want = self._resolve(company)
        raw = company_key(company)
        for r in self._db.execute(f"SELECT company, company_key, title, req_id {base}{extra}", args).fetchall():
            if self._resolve(r["company"] or r["company_key"]) != want:
                continue
            sim = title_similarity(title, r["title"])
            if sim > 0 and sim >= self.title_threshold:
                rule = "exact" if (r["company_key"] == raw) else "alias"
                return f"title~{sim:.2f} company={rule}" + ("" if title_only else " legacy-row")
        return None

    async def record(self, result: SubmitResult, *, company: str, title: str, url: str | None = None) -> None:
        await self._a(self._record, result, company, title, url)

    def _record(self, r: SubmitResult, company: str, title: str, url: str | None = None) -> None:
        now = _now()
        rid, uk = identity_key(r.ref), canonical_url(url)
        cur = self._db.execute("SELECT status FROM applications WHERE ref=?", (r.ref,)).fetchone()
        if cur is None:
            self._db.execute(
                "INSERT INTO applications(ref,company,title,company_key,title_key,status,via,confirmation,"
                "created_at,req_id,url_key) VALUES(?,?,?,?,?,?,?,?,?,?,?)",
                (r.ref, company, title, company_key(company), title_key(title), r.status, r.via,
                 r.confirmation, now, rid, uk))
        else:
            # created_at moves only when the status changes, so a dry_run later promoted to
            # 'submitted' starts its dedupe window at the real submission time.
            self._db.execute(
                "UPDATE applications SET company=?,title=?,company_key=?,title_key=?,status=?,via=?,"
                "confirmation=?,created_at=CASE WHEN status<>? THEN ? ELSE created_at END,"
                "req_id=COALESCE(?,req_id),url_key=COALESCE(?,url_key) WHERE ref=?",
                (company, title, company_key(company), title_key(title), r.status, r.via, r.confirmation,
                 r.status, now, rid, uk, r.ref))
        self._db.execute("INSERT INTO events(ref,ts,kind,detail) VALUES(?,?,?,?)",
                         (r.ref, now, r.status, r.detail))

    # -- queries ------------------------------------------------------------------------------
    async def get(self, ref: str) -> dict[str, Any] | None:
        def f():
            row = self._db.execute("SELECT * FROM applications WHERE ref=?", (ref,)).fetchone()
            return dict(row) if row else None
        return await self._a(f)

    async def events(self, ref: str) -> list[dict[str, Any]]:
        def f():
            return [dict(r) for r in self._db.execute(
                "SELECT ts,kind,detail FROM events WHERE ref=? ORDER BY id", (ref,))]
        return await self._a(f)

    async def list(self, *, status: str = "", company: str = "", since: str = "", limit: int = 50,
                   cursor: int = 0) -> dict[str, Any]:
        """Newest first. Returns {'rows': [...], 'next': int|None, 'total': int}; `cursor` is an offset."""
        limit = max(1, min(limit, 500))
        cursor = max(0, cursor)

        def f():
            where, args = [], []
            if status:
                where.append("status=?")
                args.append(status)
            if company:
                where.append("company_key LIKE ?")
                args.append(f"%{company_key(company)}%")
            if since:
                where.append("created_at>=?")
                args.append(since)
            w = (" WHERE " + " AND ".join(where)) if where else ""
            total = self._db.execute(f"SELECT COUNT(*) FROM applications{w}", args).fetchone()[0]
            rows = self._db.execute(
                f"SELECT ref,company,title,status,via,confirmation,created_at FROM applications{w} "
                "ORDER BY created_at DESC, ref LIMIT ? OFFSET ?", (*args, limit, cursor)).fetchall()
            nxt = cursor + limit if cursor + limit < total else None
            return {"rows": [dict(r) for r in rows], "next": nxt, "total": total}
        return await self._a(f)

    # -- CSV ----------------------------------------------------------------------------------
    async def export_csv(self, path: str | Path) -> int:
        def f():
            rows = self._db.execute(f"SELECT {','.join(CSV_COLUMNS)} FROM applications ORDER BY created_at"
                                    ).fetchall()
            with open(Path(path).expanduser(), "w", newline="", encoding="utf-8") as fh:
                w = csv.writer(fh)
                w.writerow(CSV_COLUMNS)
                w.writerows([tuple(r) for r in rows])
            return len(rows)
        return await self._a(f)

    async def import_csv(self, path: str | Path) -> int:
        """Upsert rows by ref (never overwrites an existing row). Returns the number of new rows."""
        def f():
            n = 0
            with open(Path(path).expanduser(), newline="", encoding="utf-8") as fh:
                for row in csv.DictReader(fh):
                    ref = (row.get("ref") or "").strip()
                    if not ref:
                        continue
                    c, t = row.get("company", ""), row.get("title", "")
                    cur = self._db.execute(
                        "INSERT OR IGNORE INTO applications(ref,company,title,company_key,title_key,status,"
                        "via,confirmation,created_at) VALUES(?,?,?,?,?,?,?,?,?)",
                        (ref, c, t, company_key(c), title_key(t), row.get("status") or "submitted",
                         row.get("via") or None, row.get("confirmation") or None,
                         row.get("created_at") or _now()))
                    n += cur.rowcount
            return n
        return await self._a(f)

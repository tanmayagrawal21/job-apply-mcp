"""One-time migration of blocking security state to the fixed state dir (ADR-0054). Imports may only TIGHTEN.

Before this release the approvals/submission book, the dedupe store, the control audit (quota) log and grants/consents
followed `paths.data_dir`. They now live under a fixed state dir. Starting empty would forget `submitting`/`unknown`/
`submitted` postings (a double-submit risk) and today's quota usage, so the previous locations are merged IN. Because
a project file or the environment can point a data dir anywhere, sources are TRUSTED locations only: the previous
default dir under the account home and `paths.data_dir` of the TRUSTED user config; each must be a real directory
(not a symlink) owned by this user, and no file inside may be a symlink. Env- and project-derived dirs are ignored.
What is imported:
  - approvals: NOTHING (never insert, never approve, not even denied/used);
  - submissions / applications: only blocking states (submitted, unknown, submitting, confirmed, ...);
  - the quota log: merged by line, except `submit_release` lines, which give quota back and are never imported;
  - grants/consents: only to tighten a row that ALREADY exists in the fixed store, and only if the old row's HMAC
    verifies against the fixed key (expiry min, uses max); no row is added; revoked ids are not revived;
  - `grant.key` and high-water files are never copied.
Files are copied from, never moved or deleted. Any failure raises `StateMigrationError`: the stores refuse to open
and `doctor` says why.
"""
from __future__ import annotations

import contextlib
import json
import logging
import os
import sqlite3
import tomllib
from pathlib import Path
from typing import Any

from . import paths

log = logging.getLogger(__name__)
MARKER = ".state_migrated.json"
_ROW_RANK = {"planned": 0, "approved": 1, "failed": 2, "handoff": 3, "awaiting_code": 4, "submitting": 5,
             "unknown": 6, "submitted": 7, "confirmed": 8}


class StateMigrationError(RuntimeError):
    """Old security state exists but could not be merged: refuse to submit (fail closed)."""


_done: dict[tuple[str, tuple[str, ...]], str | None] = {}


def _trusted_data_dir() -> Path | None:
    from .config import _user_file_trusted
    f = paths.trusted_config_file()
    try:
        if not f.is_file() or not _user_file_trusted(f)[0]:
            return None
        d = tomllib.loads(f.read_text(encoding="utf-8")).get("paths", {}).get("data_dir")
    except (OSError, ValueError, AttributeError):
        return None
    return Path(d).expanduser() if isinstance(d, str) and d else None


def _safe_source(d: Path) -> bool:
    """A real directory (no symlink in the path itself or among the files read), owned by this user."""
    try:
        if d.is_symlink() or not d.is_dir():
            return False
        if os.name != "nt" and d.stat().st_uid != os.getuid():
            return False
        names = ("approvals.db", "applications.db", "control_audit.jsonl", "control_audit.jsonl.1",
                 "grants/grants.json", "grants/consents.json")
        for rel in names:
            f = d / rel
            if f.is_symlink() or (f.exists() and (not f.is_file()
                                                  or (os.name != "nt" and f.stat().st_uid != os.getuid()))):
                return False
        return not (d / "grants").is_symlink()
    except OSError:
        return False


def old_data_dirs(settings: Any, new: Path) -> list[Path]:
    """Trusted previous locations only: the old default dir and the TRUSTED config's paths.data_dir."""
    cands = [paths.state_dir()]
    if td := _trusted_data_dir():
        cands.append(td)
    out: list[Path] = []
    for c in cands:
        try:
            r = c.resolve()
        except OSError:
            continue
        if r != new.resolve() and r not in out and _safe_source(c) and _safe_source(r):
            out.append(r)
    return out


def _fingerprint(d: Path) -> str:
    parts = []
    for rel in ("approvals.db", "approvals.db-wal", "applications.db", "applications.db-wal", "control_audit.jsonl",
                "control_audit.jsonl.1", "grants/grants.json", "grants/consents.json"):
        with contextlib.suppress(OSError):
            st = (d / rel).stat()
            parts.append(f"{rel}:{st.st_size}:{int(st.st_mtime)}")
    return "|".join(parts)


# ---------------------------------------------------------------- approvals.db
def _merge_approvals(src: Path, dst: Path) -> None:
    from .apply.approvals import _SCHEMA, BLOCKING
    dst.parent.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(str(dst), timeout=10)
    try:
        con.executescript(_SCHEMA)
        con.execute("ATTACH DATABASE ? AS old", (str(src),))
        old_tables = {r[0] for r in con.execute("SELECT name FROM old.sqlite_master WHERE type='table'")}
        marks = ",".join("?" * len(BLOCKING))
        if "submissions" in old_tables:
            for row in con.execute(f"SELECT * FROM old.submissions WHERE state IN ({marks})", BLOCKING).fetchall():
                cur = con.execute("SELECT state FROM main.submissions WHERE ref=?", (row[0],)).fetchone()
                if cur is None:
                    con.execute("INSERT INTO main.submissions VALUES(?,?,?,?,?,?,?,?,?)", row)
                elif _ROW_RANK.get(row[1], 0) > _ROW_RANK.get(cur[0], 0):
                    con.execute("DELETE FROM main.submissions WHERE ref=?", (row[0],))
                    con.execute("INSERT INTO main.submissions VALUES(?,?,?,?,?,?,?,?,?)", row)
            if "reconcile" in old_tables:
                con.execute(f"INSERT OR IGNORE INTO main.reconcile SELECT * FROM old.reconcile WHERE ref IN "
                            f"(SELECT ref FROM main.submissions WHERE state IN ({marks}))", BLOCKING)
        con.commit()
        con.execute("DETACH DATABASE old")
    finally:
        con.close()


# ---------------------------------------------------------------- applications.db (dedupe)
def _merge_applications(src: Path, dst: Path) -> None:
    from .store.sqlite import SEEN_STATUSES, SqliteStore
    SqliteStore(dst.parent, filename=dst.name).close()          # create/upgrade the destination schema
    con = sqlite3.connect(str(dst), timeout=10)
    try:
        con.execute("ATTACH DATABASE ? AS old", (str(src),))
        if not con.execute("SELECT 1 FROM old.sqlite_master WHERE type='table' AND name='applications'").fetchone():
            return
        mcols = [r[1] for r in con.execute('PRAGMA main.table_info("applications")')]
        ocols = {r[1] for r in con.execute('PRAGMA old.table_info("applications")')}
        cols = [c for c in mcols if c in ocols]
        lst = ",".join(f'"{c}"' for c in cols)
        marks = ",".join("?" * len(SEEN_STATUSES))
        con.execute(f'INSERT OR IGNORE INTO main.applications({lst}) SELECT {lst} FROM old.applications '
                    f"WHERE status IN ({marks})", SEEN_STATUSES)
        con.execute(f"UPDATE main.applications SET status=(SELECT o.status FROM old.applications o WHERE "
                    f"o.ref=main.applications.ref) WHERE status NOT IN ({marks}) AND ref IN "
                    f"(SELECT ref FROM old.applications WHERE status IN ({marks}))", (*SEEN_STATUSES, *SEEN_STATUSES))
        con.commit()
        con.execute("DETACH DATABASE old")
    finally:
        con.close()


# ---------------------------------------------------------------- control audit (quota) log
def _merge_audit_log(srcs: list[Path], dst: Path) -> None:
    from .control.controls import RELEASE
    lines: dict[str, float] = {}
    for f in (*srcs, dst):
        try:
            text = f.read_text(encoding="utf-8")
        except FileNotFoundError:
            continue
        for ln in text.splitlines():
            ln = ln.strip()
            if not ln:
                continue
            try:
                rec = json.loads(ln)
                ts = float(rec.get("ts", 0))
                if f is not dst and rec.get("a") == RELEASE:
                    continue                   # a planted release would give quota back
            except (ValueError, AttributeError, TypeError):
                ts = 0.0
            lines.setdefault(ln, ts)
    if not lines:
        return
    dst.parent.mkdir(parents=True, exist_ok=True)
    tmp = dst.with_suffix(".mig.tmp")
    tmp.write_text("\n".join(k for k, _ in sorted(lines.items(), key=lambda kv: kv[1])) + "\n", encoding="utf-8")
    os.replace(tmp, dst)


# ---------------------------------------------------------------- grants / consents
def _merge_signed_rows(store_cls: Any, src_dir: Path, new_dir: Path, **kw: Any) -> None:
    """Tighten rows that already exist, using old rows whose HMAC verifies against the FIXED key."""
    old, new = store_cls(src_dir, **kw), store_cls(new_dir, **kw)
    key = new._key()
    if key is None:
        return                                          # no fixed key: nothing can verify, so nothing is imported
    byid = {r.get("id"): r for r in new._read_all() if isinstance(r, dict)}
    changed = False
    for r in old._read_all():
        if not isinstance(r, dict):
            continue
        try:
            new._verify_row(r)                          # signature, structure and revocation, against the fixed key
        except Exception as e:  # noqa: BLE001
            log.info("state migration: dropped an old grant/consent row (%s)", type(e).__name__)
            continue
        gid = r["id"]
        cur = byid.get(gid)
        if cur is not None and cur != r:           # never add a grant or consent; only tighten one that exists
            merged = {k: v for k, v in cur.items() if k != "sig"}
            merged["expires"] = min(cur.get("expires", 0), r.get("expires", 0))
            merged["used"] = max(int(cur.get("used", 0)), int(r.get("used", 0)))
            rec = new.record_cls(**merged)
            if rec.payload() != new.record_cls(**{k: v for k, v in cur.items()}).payload():
                byid[gid] = {**json.loads(rec.payload()), "sig": _sign_payload(key, rec.payload())}
                changed = True
    if changed:
        new._write_all(list(byid.values()))
        new.audit_safe("migrated", "", "", "merged verified record(s) from an older state dir")


def _sign_payload(key: bytes, payload: bytes) -> str:
    from .grants.store import _sign
    return _sign(key, payload)


def _run(settings: Any, new: Path) -> list[str]:
    from .consent.store import ConsentStore
    from .grants.store import GrantStore
    use_kr = bool(getattr(getattr(settings, "grants", None), "use_keyring", True))
    done: list[str] = []
    marker = new / MARKER
    try:
        seen = json.loads(marker.read_text())
    except (OSError, ValueError):
        seen = {}
    for src in old_data_dirs(settings, new):
        fp = _fingerprint(src)
        tag = str(src)
        if not fp or seen.get(tag) == fp:
            continue
        if (src / "approvals.db").is_file():
            _merge_approvals(src / "approvals.db", new / "approvals.db")
        if (src / "applications.db").is_file():
            _merge_applications(src / "applications.db", new / "applications.db")
        _merge_audit_log([src / "control_audit.jsonl.1", src / "control_audit.jsonl"], new / "control_audit.jsonl")
        if (src / "grants").is_dir():
            _merge_signed_rows(GrantStore, src, new, use_keyring=use_kr)
            _merge_signed_rows(ConsentStore, src, new, use_keyring=use_kr)
        seen[tag] = _fingerprint(src)
        done.append(str(src))
        log.warning("state migration: merged tightening-only state from %s into %s (copied, nothing deleted)", src, new)
    if done:
        new.mkdir(parents=True, exist_ok=True)
        marker.write_text(json.dumps(seen))
    return done


def ensure_state(settings: Any, new: Path) -> None:
    """Idempotent; runs once per process per (state dir, candidate set). Raises StateMigrationError on failure."""
    key = (str(new), tuple(str(p) for p in old_data_dirs(settings, new)))
    if key in _done:
        if _done[key]:
            raise StateMigrationError(_done[key] or "")
        return
    try:
        _run(settings, new)
        _done[key] = None
    except StateMigrationError as e:
        _done[key] = str(e)
        raise
    except Exception as e:
        msg = f"could not merge existing approval/quota/consent state into {new}: {type(e).__name__}: {e}"
        _done[key] = msg
        raise StateMigrationError(msg) from e


# ---------------------------------------------------------------- ignored (untrusted) dirs: block, never import silently
def _untrusted_dirs(settings: Any, new: Path) -> list[Path]:
    """Env/XDG/project-chosen dirs that earlier releases may have used. NEVER imported automatically."""
    trusted = {p for p in old_data_dirs(settings, new)}
    files = [paths.config_file(), paths.config_dir() / "config.toml", Path.cwd() / "job-apply-mcp.toml"]
    cands = [Path(settings.paths.data_dir).expanduser(), paths.data_dir()]
    for f in files:
        with contextlib.suppress(OSError, ValueError, AttributeError):
            d = tomllib.loads(f.read_text(encoding="utf-8")).get("paths", {}).get("data_dir")
            if isinstance(d, str) and d:
                cands.append(Path(d).expanduser())
    out: list[Path] = []
    for c in cands:
        with contextlib.suppress(OSError):
            r = c.resolve()
            if r != new.resolve() and r not in trusted and r not in out and r.is_dir():
                out.append(r)
    return out


def _ro(db: Path) -> sqlite3.Connection:
    return sqlite3.connect(f"file:{db.as_posix()}?mode=ro", uri=True, timeout=5)


def _blocking_refs(d: Path) -> set[str]:
    from .apply.approvals import BLOCKING
    from .store.sqlite import SEEN_STATUSES
    refs: set[str] = set()
    n_b, n_s = ",".join("?" * len(BLOCKING)), ",".join("?" * len(SEEN_STATUSES))
    specs = [("approvals.db", f"SELECT ref FROM submissions WHERE state IN ({n_b})", BLOCKING),
             ("applications.db", f"SELECT ref FROM applications WHERE status IN ({n_s})", SEEN_STATUSES)]
    for fn, q, args in specs:
        f = d / fn
        if not f.is_file():
            continue
        try:
            con = _ro(f)
            try:
                tbl = "submissions" if fn == "approvals.db" else "applications"
                if con.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (tbl,)).fetchone():
                    refs |= {r[0] for r in con.execute(q, tuple(args))}
            finally:
                con.close()
        except sqlite3.Error:
            refs.add(f"<unreadable {fn}>")             # cannot prove it is empty: treat as pending
    return refs


def pending_old_state(settings: Any) -> list[tuple[Path, int]]:
    """[(dir, n)] for ignored dirs holding blocking submission/dedupe records the state dir does not have.
    Live submits are refused while this is non-empty; `job-apply-mcp migrate --from DIR` (TTY) resolves it."""
    from .config import state_path
    new = state_path(settings)
    have = _blocking_refs(new)
    out = []
    for d in _untrusted_dirs(settings, new):
        missing = _blocking_refs(d) - have
        if missing:
            out.append((d, len(missing)))
    return out


def pending_message(pending: list[tuple[Path, int]]) -> str:
    d = pending[0][0]
    return (f"old state found at {d}; run `job-apply-mcp migrate --from {d}` in a terminal to import its blocking "
            f"records (live submits are blocked until then)")


def cli(argv: list[str], settings: Any, *, stdin: Any = None, stdout: Any = None) -> int:
    """HUMAN-ONLY: `job-apply-mcp migrate --from DIR` imports blocking submission/dedupe records and quota lines."""
    import argparse
    import sys

    from .config import state_path
    stdin, stdout = stdin or sys.stdin, stdout or sys.stdout
    p = argparse.ArgumentParser(prog="job-apply-mcp migrate", description=cli.__doc__)
    p.add_argument("--from", dest="src", required=True, type=Path)
    try:
        a = p.parse_args(argv)
    except SystemExit as e:
        return int(e.code or 2)
    if not (getattr(stdin, "isatty", lambda: False)() and getattr(stdout, "isatty", lambda: False)()):
        print("refused: `migrate` is human-only and needs an interactive terminal (stdin and stdout must be a TTY)",
              file=stdout)
        return 3
    new = state_path(settings)
    src = a.src.expanduser()
    if not _safe_source(src):
        print(f"refused: {src} is not a real directory owned by you (no symlinks)", file=stdout)
        return 1
    src = src.resolve()
    try:
        if (src / "approvals.db").is_file():
            _merge_approvals(src / "approvals.db", new / "approvals.db")
        if (src / "applications.db").is_file():
            _merge_applications(src / "applications.db", new / "applications.db")
        _merge_audit_log([src / "control_audit.jsonl.1", src / "control_audit.jsonl"], new / "control_audit.jsonl")
    except (sqlite3.Error, OSError) as e:
        print(f"migrate failed: {type(e).__name__}: {e}", file=stdout)
        return 1
    print(f"imported blocking submission/dedupe records and quota lines from {src} (approvals, consents, grants and "
          f"keys are never imported)", file=stdout)
    return 0

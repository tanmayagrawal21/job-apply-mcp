"""One-time, additive migration of the security-state stores to the fixed state dir (ADR-0054).

Before 0.1.x-next the approvals/submission book, control audit (quota) log, grants/consents and their key/high-water
files followed XDG_DATA_HOME, `paths.data_dir` or the XDG config dir. They now live under a fixed state dir. Starting
empty would forget `submitting`/`unknown`/`submitted` postings (a double-submit risk) and today's quota usage, so on
first use every OLD location is discovered read-only (never trusted: only used as a copy source) and UNIONED in. The
old files are copied, never moved or deleted. When both sides have a record the most restrictive wins. Any failure
raises `StateMigrationError`: the stores refuse to open (no submit) and `doctor` shows the reason.
"""
from __future__ import annotations

import contextlib
import json
import logging
import os
import shutil
import sqlite3
import tomllib
from pathlib import Path
from typing import Any

from . import paths

log = logging.getLogger(__name__)
MARKER = ".state_migrated.json"
_ROW_RANK = {"planned": 0, "approved": 1, "failed": 2, "handoff": 3, "awaiting_code": 4, "submitting": 5,
             "unknown": 6, "submitted": 7, "confirmed": 8}
_APPR_RANK = {"pending": 0, "approved": 1, "denied": 2, "used": 3}


class StateMigrationError(RuntimeError):
    """Old security state exists but could not be merged: refuse to submit (fail closed)."""


_done: dict[tuple[str, tuple[str, ...]], str | None] = {}


def _cfg_data_dirs(files: list[Path]) -> list[Path]:
    out = []
    for f in files:
        try:
            d = tomllib.loads(f.read_text(encoding="utf-8")).get("paths", {}).get("data_dir")
        except (OSError, ValueError, AttributeError):
            continue
        if isinstance(d, str) and d:
            out.append(Path(d).expanduser())
    return out


def old_data_dirs(settings: Any, new: Path) -> list[Path]:
    """Every place the previous releases could have kept the stores. Discovery only; nothing here is trusted."""
    files = [paths.trusted_config_file(), paths.config_file(), paths.config_dir() / "config.toml",
             Path.cwd() / "job-apply-mcp.toml"]
    cands = [Path(settings.paths.data_dir).expanduser(), paths.data_dir(), paths.state_dir(),
             *_cfg_data_dirs(files)]
    seen: list[Path] = []
    for c in cands:
        try:
            r = c.resolve()
        except OSError:
            continue
        if r != new.resolve() and r not in seen and r.is_dir():
            seen.append(r)
    return seen


def old_config_dirs() -> list[Path]:
    fixed = paths.fixed_config_dir()
    out: list[Path] = []
    for c in (paths.config_dir(),):
        with contextlib.suppress(OSError):
            if c.resolve() != fixed.resolve() and c.is_dir():
                out.append(c)
    return out


def _fingerprint(d: Path) -> str:
    parts = []
    for rel in ("approvals.db", "approvals.db-wal", "applications.db", "applications.db-wal", "control_audit.jsonl", "control_audit.jsonl.1",
                "grants/grants.json", "grants/consents.json", "grants/audit.jsonl", "grants/consent_audit.jsonl"):
        with contextlib.suppress(OSError):
            st = (d / rel).stat()
            parts.append(f"{rel}:{st.st_size}:{int(st.st_mtime)}")
    return "|".join(parts)


# ---------------------------------------------------------------- approvals.db
def _merge_approvals(src: Path, dst: Path) -> None:
    from .apply.approvals import _SCHEMA  # same schema both sides
    dst.parent.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(str(dst), timeout=10)
    try:
        con.executescript(_SCHEMA)
        con.execute("ATTACH DATABASE ? AS old", (str(src),))
        old_tables = {r[0] for r in con.execute("SELECT name FROM old.sqlite_master WHERE type='table'")}
        if "submissions" in old_tables:
            for row in con.execute("SELECT * FROM old.submissions").fetchall():
                ref, state = row[0], row[1]
                cur = con.execute("SELECT state FROM main.submissions WHERE ref=?", (ref,)).fetchone()
                if cur is None:
                    con.execute("INSERT INTO main.submissions VALUES(?,?,?,?,?,?,?,?,?)", row)
                elif _ROW_RANK.get(state, 0) > _ROW_RANK.get(cur[0], 0):
                    con.execute("DELETE FROM main.submissions WHERE ref=?", (ref,))
                    con.execute("INSERT INTO main.submissions VALUES(?,?,?,?,?,?,?,?,?)", row)
        if "approvals" in old_tables:
            for row in con.execute("SELECT * FROM old.approvals").fetchall():
                cur = con.execute("SELECT status FROM main.approvals WHERE approval_id=?", (row[0],)).fetchone()
                if cur is None:
                    con.execute("INSERT INTO main.approvals VALUES(?,?,?,?,?,?,?,?,?,?,?,?)", row)
                elif _APPR_RANK.get(row[8], 0) > _APPR_RANK.get(cur[0], 0):
                    con.execute("UPDATE main.approvals SET status=?, approved_by=? WHERE approval_id=?",
                                (row[8], row[9], row[0]))
        if "reconcile" in old_tables:
            con.execute("INSERT OR IGNORE INTO main.reconcile SELECT * FROM old.reconcile")
        if "approval_audit" in old_tables:
            con.execute("INSERT INTO main.approval_audit(ts,ref,approval_id,event,actor,detail) "
                        "SELECT o.ts,o.ref,o.approval_id,o.event,o.actor,o.detail FROM old.approval_audit o "
                        "WHERE NOT EXISTS (SELECT 1 FROM main.approval_audit m WHERE m.ts=o.ts AND m.event=o.event "
                        "AND IFNULL(m.ref,'')=IFNULL(o.ref,'') AND IFNULL(m.approval_id,'')=IFNULL(o.approval_id,''))")
        con.commit()
        con.execute("DETACH DATABASE old")
    finally:
        con.close()


# ---------------------------------------------------------------- applications.db (dedupe store + intake tables)
def _merge_applications(src: Path, dst: Path) -> None:
    from .store.sqlite import SEEN_STATUSES, SqliteStore
    SqliteStore(dst.parent, filename=dst.name).close()          # create/upgrade the destination schema
    con = sqlite3.connect(str(dst), timeout=10)
    try:
        con.execute("ATTACH DATABASE ? AS old", (str(src),))
        for name, ddl in con.execute("SELECT name, sql FROM old.sqlite_master WHERE type='table' AND sql IS NOT NULL "
                                     "AND name NOT LIKE 'sqlite_%'").fetchall():
            if not con.execute("SELECT 1 FROM main.sqlite_master WHERE type='table' AND name=?", (name,)).fetchone():
                con.execute(ddl)
            mcols = [r[1] for r in con.execute(f'PRAGMA main.table_info("{name}")')]
            ocols = {r[1] for r in con.execute(f'PRAGMA old.table_info("{name}")')}
            pk = [r[1] for r in con.execute(f'PRAGMA main.table_info("{name}")') if r[5]]
            cols = [c for c in mcols if c in ocols]
            if pk == ["id"] and "id" in cols:                    # autoincrement log table: append rows not present
                cols.remove("id")
                lst = ",".join(f'"{c}"' for c in cols)
                con.execute(f'INSERT INTO main."{name}"({lst}) SELECT {lst} FROM old."{name}" '
                            f'EXCEPT SELECT {lst} FROM main."{name}"')
            elif cols:
                lst = ",".join(f'"{c}"' for c in cols)
                con.execute(f'INSERT OR IGNORE INTO main."{name}"({lst}) SELECT {lst} FROM old."{name}"')
        marks = ",".join("?" * len(SEEN_STATUSES))
        old_apps = {r[1] for r in con.execute('PRAGMA old.table_info("applications")')}
        if "ref" in old_apps and "status" in old_apps:           # an application that really went out always wins
            con.execute(f"UPDATE main.applications SET status=(SELECT o.status FROM old.applications o "
                        f"WHERE o.ref=main.applications.ref) WHERE status NOT IN ({marks}) AND ref IN "
                        f"(SELECT ref FROM old.applications WHERE status IN ({marks}))", (*SEEN_STATUSES, *SEEN_STATUSES))
        con.commit()
        con.execute("DETACH DATABASE old")
    finally:
        con.close()


# ---------------------------------------------------------------- control audit (quota) log
def _merge_audit_log(srcs: list[Path], dst: Path) -> None:
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
                ts = float(json.loads(ln).get("ts", 0))
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
    old, new = store_cls(src_dir, **kw), store_cls(new_dir, **kw)
    for name in new.files[1:]:        # audit chain: copy only when the new side has none (chains cannot be unioned)
        s, d = src_dir / "grants" / name, new_dir / "grants" / name
        if s.is_file() and not d.exists():
            d.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(s, d)
    orows = old._read_all()
    if not orows:
        return
    nrows = new._read_all()
    byid = {r.get("id"): r for r in nrows if isinstance(r, dict)}
    key = new._key()
    changed = False
    for r in orows:
        gid = r.get("id") if isinstance(r, dict) else None
        if not isinstance(gid, str) or new._is_revoked(gid):
            continue                                   # never revive a revoked grant or consent
        cur = byid.get(gid)
        if cur is None:
            byid[gid] = r
            changed = True
        elif cur != r:
            if key is None:
                raise StateMigrationError(f"grant {gid} differs on both sides and the signing key is unavailable")
            merged = {k: v for k, v in cur.items() if k != "sig"}
            merged["expires"] = min(cur.get("expires", 0), r.get("expires", 0))      # earliest expiry
            merged["used"] = max(int(cur.get("used", 0)), int(r.get("used", 0)))      # fewest uses left
            rec = new.record_cls(**merged)
            byid[gid] = {**json.loads(rec.payload()), "sig": _sign_payload(key, rec.payload())}
            changed = True
    if changed:
        new._write_all(list(byid.values()))
        new.audit_safe("migrated", "", "", f"merged {len(orows)} record(s) from an older state dir")


def _sign_payload(key: bytes, payload: bytes) -> str:
    from .grants.store import _sign
    return _sign(key, payload)


# ---------------------------------------------------------------- key + high-water files (old config dir)
def _merge_config_side(old: Path, fixed: Path) -> None:
    for name in ("grant.key",):
        s, d = old / name, fixed / name
        if s.is_file() and not d.exists():
            d.parent.mkdir(parents=True, exist_ok=True)
            fd = os.open(d, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            with os.fdopen(fd, "w") as fh:
                fh.write(s.read_text())
    for name in ("grant_uses.json", "consent_uses.json", "revoked_grant_uses.json", "revoked_consent_uses.json"):
        s, d = old / name, fixed / name
        if not s.is_file():
            continue
        try:
            a = json.loads(s.read_text())
            b = json.loads(d.read_text()) if d.is_file() else ([] if name.startswith("revoked") else {})
        except ValueError as e:
            raise StateMigrationError(f"{s} is unreadable") from e
        merged: Any
        if isinstance(a, list):
            merged = sorted({*a, *(b if isinstance(b, list) else [])})
        else:
            merged = {**b, **{k: max(v, b.get(k, 0)) for k, v in a.items() if isinstance(v, int)}}
        d.parent.mkdir(parents=True, exist_ok=True)
        tmp = d.with_suffix(".mig.tmp")
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w") as fh:
            fh.write(json.dumps(merged))
        os.replace(tmp, d)


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
    for cdir in old_config_dirs():
        _merge_config_side(cdir, paths.fixed_config_dir())
    for src in old_data_dirs(settings, new):
        fp = _fingerprint(src)
        if not fp or seen.get(str(src)) == fp:
            continue
        if (src / "approvals.db").is_file():
            _merge_approvals(src / "approvals.db", new / "approvals.db")
        if (src / "applications.db").is_file():
            _merge_applications(src / "applications.db", new / "applications.db")
        _merge_audit_log([src / "control_audit.jsonl.1", src / "control_audit.jsonl"], new / "control_audit.jsonl")
        if (src / "grants").is_dir():
            _merge_signed_rows(GrantStore, src, new, use_keyring=use_kr)
            _merge_signed_rows(ConsentStore, src, new, use_keyring=use_kr)
        seen[str(src)] = _fingerprint(src)
        done.append(str(src))
        log.warning("state migration: merged security state from %s into %s (copied, nothing deleted)", src, new)
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

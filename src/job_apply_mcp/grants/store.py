"""Signed grant records, HMAC secret handling and the hash-chained audit log (ADR-0027).

A grant is {id, scope, os_user, created, expires, max_uses, nonce, used} + HMAC-SHA256 signature. `used` is the
consumed-use counter: it is bumped and re-signed on every use, so truncating the audit log cannot hand uses back
(ADR-0037). The count comes only from that counter; a grant id stored twice is rejected as tampering. The secret lives in the
OS keychain (optional `keyring` extra) or a 0600 file in the user config dir; never in the repo, project or env.
Only the CLI (`job-apply-mcp grant`, interactive TTY) creates grants; MCP can only read them.
"""
from __future__ import annotations

import contextlib
import getpass
import hashlib
import hmac
import json
import logging
import os
import re
import secrets
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .. import paths

log = logging.getLogger(__name__)
KEYRING_SERVICE = "job-apply-mcp"
KEYRING_USER = "grant-hmac-secret"

SCOPES: dict[str, str] = {
    "approve_submissions": ("The AI agent may APPROVE application submissions itself (approval_mode=human) for the "
                            "window: it can submit real applications without a per-application human click. "
                            "Each approval is logged with the grant id."),
    "otp_assist": ("When a site asks for an emailed verification code, the server may read ONLY the single most recent "
                   "message from the expected sender (last few minutes) and show you the masked code to confirm. "
                   "The code is never submitted without your click, and never stored or logged."),
}


class GrantError(Exception):
    """Raised when a grant is missing, expired, tampered, exhausted or for another user."""


_DUR = re.compile(r"(\d+)\s*([smhd])", re.IGNORECASE)


def parse_duration(text: str) -> int:
    """'90s', '30m', '2h', '1h30m', '1d' -> seconds. Bare number = minutes."""
    t = text.strip().lower()
    if t.isdigit():
        return int(t) * 60
    parts = _DUR.findall(t)
    if not parts or "".join(f"{n}{u}" for n, u in parts) != re.sub(r"\s+", "", t):
        raise ValueError(f"bad duration {text!r}; use e.g. 30m, 2h, 1h30m")
    mult = {"s": 1, "m": 60, "h": 3600, "d": 86400}
    return sum(int(n) * mult[u] for n, u in parts)


# ------------------------------------------------------------------ secret
def _secret_file() -> Path:
    return paths.config_dir() / "grant.key"


def load_secret(*, use_keyring: bool = True, create: bool = True, key_file: Path | None = None) -> bytes:
    """Per-install HMAC secret: OS keychain first, else a 0600 file in the user config dir."""
    if use_keyring:
        try:
            import keyring  # lazy: optional extra
            val = keyring.get_password(KEYRING_SERVICE, KEYRING_USER)
            if val:
                return bytes.fromhex(val)
            if create:
                new = secrets.token_hex(32)
                keyring.set_password(KEYRING_SERVICE, KEYRING_USER, new)
                return bytes.fromhex(new)
        except Exception as e:  # noqa: BLE001 - no keyring / no backend / locked: fall back to the file
            log.debug("keyring unavailable (%s); using file secret", type(e).__name__)
    f = key_file or _secret_file()
    if f.is_file():
        return bytes.fromhex(f.read_text().strip())
    if not create:
        raise GrantError("no grant secret exists yet")
    f.parent.mkdir(parents=True, exist_ok=True)
    new = secrets.token_hex(32)
    fd = os.open(f, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "w") as fh:
        fh.write(new)
    return bytes.fromhex(new)


# ------------------------------------------------------------------ records
@dataclass(frozen=True)
class Grant:
    id: str
    scope: str
    os_user: str
    created: float
    expires: float
    max_uses: int | None
    nonce: str
    used: int = 0
    sig: str = ""

    def payload(self) -> bytes:
        d = {"id": self.id, "scope": self.scope, "os_user": self.os_user, "created": self.created,
             "expires": self.expires, "max_uses": self.max_uses, "nonce": self.nonce, "used": self.used}
        return json.dumps(d, sort_keys=True, separators=(",", ":")).encode()


def _sign(secret: bytes, data: bytes) -> str:
    return hmac.new(secret, data, hashlib.sha256).hexdigest()


def os_user() -> str:
    try:
        return getpass.getuser()
    except Exception:  # noqa: BLE001
        return os.environ.get("USER", "")


class GrantStore:
    def __init__(self, data_dir: str | Path, secret: bytes | None = None, *, use_keyring: bool = True,
                 clock=time.time, user: str | None = None):
        self.dir = Path(data_dir).expanduser() / "grants"
        self._secret, self._use_keyring = secret, use_keyring
        self._clock, self._user = clock, user
        self._lock = threading.RLock()

    # secret is resolved lazily so read-only status never creates one
    def _key(self, create: bool = False) -> bytes | None:
        if self._secret is None:
            try:
                self._secret = load_secret(use_keyring=self._use_keyring, create=create)
            except GrantError:
                return None
        return self._secret

    @property
    def user(self) -> str:
        return self._user or os_user()

    @property
    def _grants_file(self) -> Path:
        return self.dir / "grants.json"

    @property
    def _audit_file(self) -> Path:
        return self.dir / "audit.jsonl"

    # ---- audit (hash-chained: prev signature is part of every line's signature)
    def _audit_lines(self) -> tuple[list[dict[str, Any]], bool]:
        """Return (valid entries, chain_ok). A broken chain means the log was edited or truncated mid-file."""
        key = self._key()
        out: list[dict[str, Any]] = []
        if not self._audit_file.is_file():
            return out, True
        prev, ok = "", True
        for line in self._audit_file.read_text().splitlines():
            try:
                e = json.loads(line)
                body = {k: v for k, v in e.items() if k != "sig"}
                good = key is not None and e.get("prev") == prev and hmac.compare_digest(
                    e.get("sig", ""), _sign(key, json.dumps(body, sort_keys=True, separators=(",", ":")).encode()))
            except (ValueError, TypeError, AttributeError):
                good = False
            if not good:
                ok = False
                continue
            prev = e["sig"]
            out.append(e)
        return out, ok

    def audit(self, event: str, grant_id: str = "", scope: str = "", detail: str = "") -> None:
        """Append a signed line. NEVER pass a verification code or other secret in `detail`."""
        key = self._key(create=True)
        assert key is not None
        with self._lock:
            self.dir.mkdir(parents=True, exist_ok=True)
            entries, _ = self._audit_lines()
            body = {"ts": round(self._clock(), 3), "event": event, "grant": grant_id, "scope": scope,
                    "user": self.user, "detail": detail[:200], "prev": entries[-1]["sig"] if entries else ""}
            body["sig"] = _sign(key, json.dumps({k: v for k, v in body.items() if k != "sig"},
                                                sort_keys=True, separators=(",", ":")).encode())
            with open(self._audit_file, "a", encoding="utf-8") as fh:
                fh.write(json.dumps(body, sort_keys=True, separators=(",", ":")) + "\n")

    def audit_rows(self) -> list[dict[str, Any]]:
        return self._audit_lines()[0]

    # ---- grants
    def _read_all(self) -> list[dict[str, Any]]:
        try:
            data = json.loads(self._grants_file.read_text())
            return data if isinstance(data, list) else []
        except (OSError, ValueError):
            return []

    def _write_all(self, rows: list[dict[str, Any]]) -> None:
        self.dir.mkdir(parents=True, exist_ok=True)
        tmp = self._grants_file.with_suffix(".tmp")
        tmp.write_text(json.dumps(rows, indent=1))
        os.replace(tmp, self._grants_file)

    def issue(self, scope: str, duration_s: int, max_uses: int | None = None) -> Grant:
        """Called by the CLI after the human confirmed. Never exposed over MCP."""
        if scope not in SCOPES:
            raise ValueError(f"unknown scope {scope!r}; known: {sorted(SCOPES)}")
        key = self._key(create=True)
        assert key is not None
        now = self._clock()
        g = Grant(id=secrets.token_hex(6), scope=scope, os_user=self.user, created=now, expires=now + duration_s,
                  max_uses=max_uses, nonce=secrets.token_hex(8))
        row = {**json.loads(g.payload()), "sig": _sign(key, g.payload())}
        with self._lock:
            self._write_all([*self._read_all(), row])
        self.audit("issued", g.id, scope, f"for={duration_s}s max_uses={max_uses}")
        return Grant(**row)

    def _verify_row(self, row: dict[str, Any]) -> Grant:
        key = self._key()
        if key is None:
            raise GrantError("grant secret unavailable")
        try:
            g = Grant(**row)
        except TypeError:
            raise GrantError("malformed grant record") from None
        if "used" not in row or not isinstance(g.used, int) or isinstance(g.used, bool) or g.used < 0:
            raise GrantError(f"grant {g.id}: malformed use counter (re-issue the grant)")
        if not hmac.compare_digest(g.sig, _sign(key, g.payload())):
            raise GrantError(f"grant {g.id}: signature invalid (tampered)")
        return g

    def _dup_ids(self, rows: list[dict[str, Any]]) -> set[str]:
        """Grant ids that appear more than once: a copied row would carry its own counter, so it is tampering."""
        seen: set[str] = set()
        dup: set[str] = set()
        for r in rows:
            gid = r.get("id")
            if isinstance(gid, str):
                (dup if gid in seen else seen).add(gid)
        return dup

    def uses(self, grant_id: str) -> int:
        """Consumed uses from the signed per-record counter only (never from the audit log)."""
        for row in self._read_all():
            if row.get("id") == grant_id:
                with contextlib.suppress(GrantError):
                    return self._verify_row(row).used
        return 0

    def _bump(self, g: Grant) -> None:
        """Persist used+1 in the signed record (caller holds the lock). Raises if the record vanished."""
        key = self._key()
        assert key is not None
        rows = self._read_all()
        for i, row in enumerate(rows):
            if row.get("id") == g.id and row.get("sig") == g.sig:
                ng = Grant(**{**json.loads(g.payload()), "used": g.used + 1})
                rows[i] = {**json.loads(ng.payload()), "sig": _sign(key, ng.payload())}
                self._write_all(rows)
                return
        raise GrantError(f"grant {g.id}: record changed during use")

    def active(self) -> list[tuple[Grant, int | None]]:
        """Valid, unexpired, same-user, unexhausted grants with remaining uses (None = unlimited). Read-only."""
        if self._key() is None:
            return []
        _, chain_ok = self._audit_lines()
        rows = self._read_all()
        dup = self._dup_ids(rows)
        now, res = self._clock(), []
        for row in rows:
            if row.get("id") in dup:
                continue
            try:
                g = self._verify_row(row)
            except GrantError:
                continue
            if g.expires <= now or g.os_user != self.user:
                continue
            left = None if g.max_uses is None else g.max_uses - g.used
            if left is not None and (left <= 0 or not chain_ok):   # cannot trust use counts: fail closed
                continue
            res.append((g, left))
        return res

    def check(self, scope: str, *, consume: bool = True, detail: str = "") -> Grant:
        """Verify signature, expiry, os_user and max_uses; audit every use (and every refusal)."""
        reason = "no active grant"
        key = self._key()
        if key is None:
            raise GrantError(f"no active grant for '{scope}'; a human must run `job-apply-mcp grant {scope} --for 1h`")
        with self._lock:
            _, chain_ok = self._audit_lines()
            rows = self._read_all()
            dup = self._dup_ids(rows)
            now = self._clock()
            for gid in sorted(i for i in dup if any(r.get("id") == i and r.get("scope") == scope for r in rows)):
                reason = f"grant {gid}: duplicated in the grant store (tampered)"
                self.audit_safe("tampered", gid, scope, "duplicate grant id")
            for row in rows:
                if row.get("scope") != scope or row.get("id") in dup:
                    continue
                try:
                    g = self._verify_row(row)
                except GrantError as e:
                    reason = str(e)
                    continue
                if g.os_user != self.user:
                    reason = f"grant {g.id}: issued for another OS user"
                elif g.expires <= now:
                    reason = f"grant {g.id}: expired"
                elif g.max_uses is not None and not chain_ok:
                    reason = f"grant {g.id}: audit log tampered; cannot verify use count"
                elif g.max_uses is not None and g.used >= g.max_uses:
                    reason = f"grant {g.id}: max_uses reached"
                else:
                    if consume:
                        self._bump(g)                 # signed counter first, then the audit line
                        self.audit("use", g.id, scope, detail)
                    return g
            self.audit_safe("denied", "", scope, reason)
        raise GrantError(f"{reason}; a human must run `job-apply-mcp grant {scope} --for 1h`")

    def audit_safe(self, *a: Any, **k: Any) -> None:
        with contextlib.suppress(Exception):    # a refusal must not turn into a crash
            self.audit(*a, **k)

    def revoke(self, which: str) -> list[str]:
        with self._lock:
            rows = self._read_all()
            gone = [r["id"] for r in rows if which == "all" or r.get("id") == which]
            if gone:
                self._write_all([r for r in rows if r.get("id") not in gone])
                for gid in gone:
                    self.audit("revoked", gid)
        return gone


def store_for(settings) -> GrantStore:
    return GrantStore(Path(settings.paths.data_dir), use_keyring=settings.grants.use_keyring)


def require_grant(settings, scope: str, *, detail: str = "") -> Grant:
    """Raise GrantError unless a valid grant for `scope` exists; consumes one use and audits it."""
    return store_for(settings).check(scope, detail=detail)


def try_use(settings, scope: str, detail: str = "") -> Grant | None:
    try:
        return require_grant(settings, scope, detail=detail)
    except GrantError:
        return None

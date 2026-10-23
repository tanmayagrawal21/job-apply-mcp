"""Consent windows (ADR-0048): one human consent per period, covering several unattended scopes.

Built on the signed grant store (ADR-0027/0037): same HMAC secret, same hash-chained audit format, same
max-of-three use counter (signed record counter, audit `use` lines, high-water mark), but its own files
(`consents.json`, `consent_audit.jsonl`, `consent_uses.json` / keychain `consent-uses:<id>`), so grants and consents
never share a counter. Only the CLI (`job-apply-mcp consent`, interactive TTY) mints a consent; MCP can only read.

Uses: only `approve_submissions` is metered (one use per submission approved), so `--max N` caps submissions.
Other scopes are checked and audited (`act` lines) but never counted, so the code step of the last allowed
submission still works.
"""
from __future__ import annotations

import contextlib
import contextvars
import json
import os
import secrets
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from ..grants.store import Grant, GrantError, GrantStore, _Ok

CONSENT_SCOPES: dict[str, str] = {
    "approve_submissions": ("Submissions are approved automatically (approval_mode=human is satisfied by this "
                            "consent). Each approval is logged with the consent id. All other gates still apply: "
                            "dry_run, policy, stop phrases, daily/run caps, dedupe."),
    "captcha_attempt": ("On pages whose ONLY bot check is a passive/invisible widget, Submit is clicked once, "
                        "like a person (ADR-0036 rules). A visible CAPTCHA challenge always stays a handoff and "
                        "is never solved."),
    "otp_unattended": ("Read verification codes from your mail and enter them: after Submit, and when a new "
                       "job-site account asks you to confirm its email (also called `mail`). Only the newest "
                       "message from the expected sender, verified by your mail server, is read; the code is "
                       "entered once and never logged, stored or returned. No usable mail -> unknown, never "
                       "retried."),
    "credentials": ("Sign in with vault passwords and create accounts with generated passwords on job sites "
                    "(ADR-0046). Passwords go from the OS keychain straight into the page, never to the model. "
                    "Account creation also needs accounts.auto_create = true in your user config."),
}
DEFAULT_SCOPES: tuple[str, ...] = tuple(CONSENT_SCOPES)
MODES = ("window", "per_run")
METERED = frozenset({"approve_submissions"})

_PROCESS_RUN = f"p{os.getpid()}-{secrets.token_hex(4)}"
_RUN: contextvars.ContextVar[str | None] = contextvars.ContextVar("job_apply_consent_run", default=None)


def current_run() -> str:
    """The run a `--per-run` consent binds to: the enclosing `run_context(...)`, else this process."""
    return _RUN.get() or _PROCESS_RUN


@contextlib.contextmanager
def run_context(run_id: str) -> Iterator[None]:
    """Optional: scope a run (e.g. one `run_applications` call) so a `--per-run` consent covers exactly it."""
    tok = _RUN.set(str(run_id))
    try:
        yield
    finally:
        _RUN.reset(tok)


@dataclass(frozen=True)
class Consent(Grant):
    scopes: tuple[str, ...] = ()
    mode: str = "window"
    run: str = ""                 # per_run: the run it is bound to ("" until its first use)

    def payload(self) -> bytes:
        d = json.loads(super().payload())
        d.update(scopes=sorted(self.scopes), mode=self.mode, run=self.run)
        return json.dumps(d, sort_keys=True, separators=(",", ":")).encode()


try:                                   # cross-process mutual exclusion for claim/use (POSIX); in-process RLock always
    import fcntl as _fcntl
except ImportError:                    # pragma: no cover - Windows
    _fcntl = None  # type: ignore[assignment]


class ConsentStore(GrantStore):
    record_cls = Consent
    files = ("consents.json", "consent_audit.jsonl")
    hwm_prefix = "consent-uses:"
    hwm_name = "consent_uses.json"

    def __init__(self, data_dir: str | Path, secret: bytes | None = None, *, run_id: str | None = None,
                 **kw: Any):
        super().__init__(data_dir, secret, **kw)
        self._run_id = run_id

    @property
    def run_id(self) -> str:
        return self._run_id or current_run()

    # ---- GrantStore hooks
    def _covers(self, row: dict[str, Any], scope: str) -> bool:
        s = row.get("scopes")
        return isinstance(s, list) and scope in s

    def _metered(self, scope: str) -> bool:
        return scope in METERED

    def _validate(self, g: Grant) -> None:
        c: Consent = g  # type: ignore[assignment]
        if (c.scope != "consent" or not isinstance(c.scopes, list | tuple) or not c.scopes
                or not set(c.scopes) <= set(CONSENT_SCOPES) or c.mode not in MODES or not isinstance(c.run, str)):
            raise GrantError(f"consent {c.id}: malformed record")

    def _usable(self, g: Grant, scope: str) -> _Ok:
        c: Consent = g  # type: ignore[assignment]
        if c.mode == "per_run" and c.run and c.run != self.run_id:
            return _Ok(False, f"consent {c.id}: per-run consent already used by another run")
        return _Ok(True)

    def _hint(self, scope: str) -> str:
        return "a human must run `job-apply-mcp consent --for 24h` (or `--per-run`) in a terminal"

    @contextlib.contextmanager
    def _xlock(self) -> Iterator[None]:
        with self._lock:
            if _fcntl is None or not self.dir.is_dir():     # nothing persisted yet: nothing to race on
                yield
                return
            fd = os.open(self.dir / ".consent.lock", os.O_RDWR | os.O_CREAT, 0o600)
            try:
                _fcntl.flock(fd, _fcntl.LOCK_EX)
                yield
            finally:
                _fcntl.flock(fd, _fcntl.LOCK_UN)
                os.close(fd)

    # ---- minting (CLI only)
    def issue(self, scope: str, duration_s: int, max_uses: int | None = None) -> Grant:
        raise TypeError("use issue_consent()")

    def issue_consent(self, duration_s: int, *, scopes: tuple[str, ...] | list[str] = DEFAULT_SCOPES,
                      mode: str = "window", max_uses: int | None = None) -> Consent:
        """Called by the CLI after the human typed the confirmation phrase. Never exposed over MCP."""
        bad = sorted(set(scopes) - set(CONSENT_SCOPES))
        if bad or not scopes:
            raise ValueError(f"unknown scope(s) {bad}; known: {sorted(CONSENT_SCOPES)}")
        if mode not in MODES:
            raise ValueError(f"mode must be one of {MODES}")
        key = self._key(create=True)
        assert key is not None
        from ..grants.store import _sign
        now = self._clock()
        c = Consent(id="c" + secrets.token_hex(6), scope="consent", os_user=self.user, created=now,
                    expires=now + duration_s, max_uses=max_uses, nonce=secrets.token_hex(8),
                    scopes=tuple(sorted(set(scopes))), mode=mode)
        row = {**json.loads(c.payload()), "sig": _sign(key, c.payload())}
        with self._xlock():
            self._write_all([*self._read_all(), row])
        self.audit("issued", c.id, "consent",
                   f"mode={mode} for={duration_s}s max={max_uses} scopes={','.join(sorted(set(scopes)))}")
        return Consent(**row)

    # ---- runtime
    def use(self, scope: str, *, detail: str = "", consume: bool = True) -> Consent:
        """Check (and, with consume, record) one unattended action under `scope`. Binds a per-run consent to this
        run on its first use. Raises GrantError when no consent covers it."""
        if scope not in CONSENT_SCOPES:
            raise ValueError(f"unknown consent scope {scope!r}")
        with self._xlock():
            self._quiet = not consume                 # a read-only probe writes no `denied` line
            try:
                c: Consent = self.check(scope, consume=consume, detail=detail)  # type: ignore[assignment]
            finally:
                self._quiet = False
            if consume and c.mode == "per_run" and not c.run:
                c = self._resign(self._reload(c.id), run=self.run_id)          # type: ignore[assignment]
                self.audit("claimed", c.id, "consent", f"run={self.run_id[:40]}")
            return c

    _quiet = False

    def audit_safe(self, event: str, *a: Any, **k: Any) -> None:
        if self._quiet and event == "denied":
            return
        super().audit_safe(event, *a, **k)

    def _reload(self, cid: str) -> Consent:
        for row in self._read_all():
            if row.get("id") == cid:
                return self._verify_row(row)                                 # type: ignore[return-value]
        raise GrantError(f"consent {cid}: record vanished")

    def end_run(self) -> list[str]:
        """Close every per-run consent bound to this run (it expires now). Returns the closed ids."""
        out: list[str] = []
        with self._xlock():
            for row in self._read_all():
                with contextlib.suppress(GrantError):
                    c: Consent = self._verify_row(row)                      # type: ignore[assignment]
                    if c.mode == "per_run" and c.run == self.run_id and c.expires > self._clock():
                        self._resign(c, expires=self._clock())
                        self.audit("run_ended", c.id, "consent")
                        out.append(c.id)
        return out

    def status(self) -> list[dict[str, Any]]:
        """Read-only: active consents. Never creates a secret, binds a run or records a use."""
        now = self._clock()
        out = []
        for g, left in self.active():
            c: Consent = g  # type: ignore[assignment]
            bound = c.mode == "per_run" and bool(c.run)
            if bound and c.run != self.run_id:
                continue
            out.append({"id": c.id, "mode": c.mode, "scopes": sorted(c.scopes), "expires_in_s": int(c.expires - now),
                        "submissions_left": left, "bound_to_this_run": bound})
        return out


def store_for(settings, run_id: str | None = None) -> ConsentStore:
    return ConsentStore(Path(settings.paths.data_dir), use_keyring=settings.grants.use_keyring, run_id=run_id)

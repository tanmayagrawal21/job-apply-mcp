"""Login walls and account creation (ADR-0046). The LLM never sees a secret.

The browser driver calls `AccountManager.handle_wall(ops, url, email=...)` when it meets a login or sign-up page.
`ops` (LoginOps) is the only thing that touches the page; the secret goes from the keychain straight into
`ops.fill_password(secret)`, and nothing that comes back (LoginOutcome, run report, logs) contains it.

Every path is gated on `consent.active(settings, "credentials")`. Order of checks:
  host stopped this run -> no consent -> visible CAPTCHA (handoff, never solved) -> vault entry -> login;
  no entry -> account creation only if accounts.auto_create (trusted user config only) and not dry_run.
"""
from __future__ import annotations

import logging
import re
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Any, Literal, Protocol

from .. import consent
from ..core.secrets import Secret
from .password import generate_password
from .store import AccountsDB
from .vault import Vault, VaultError, normalize_host

log = logging.getLogger(__name__)

PageKind = Literal["login", "signup", "none"]
LoginResult = Literal["ok", "wrong_password", "locked", "captcha", "verification", "unknown"]
SignupResult = Literal["created", "verify_email", "exists", "captcha", "unknown"]
Status = Literal["logged_in", "created", "pending_verification", "needs_human", "blocked", "skipped"]


@dataclass(frozen=True)
class Terms:
    """A checkbox on a sign-up form. Only `required` ones that read like terms/privacy are ever ticked."""
    ref: str                 # opaque handle for ops.accept_terms
    text: str
    required: bool
    url: str | None = None


class LoginOps(Protocol):
    async def captcha_visible(self) -> bool: ...
    async def kind(self) -> PageKind: ...
    async def open_signin(self) -> bool: ...
    async def open_signup(self) -> bool: ...
    async def fill_username(self, username: str) -> bool: ...
    async def fill_password(self, secret: Secret, *, confirm: bool = False) -> bool: ...
    async def terms(self) -> list[Terms]: ...
    async def accept_terms(self, t: Terms) -> bool: ...
    async def submit(self, kind: PageKind) -> bool: ...
    async def login_result(self) -> LoginResult: ...
    async def signup_result(self) -> SignupResult: ...
    def url(self) -> str: ...


@dataclass
class LoginOutcome:
    host: str
    status: Status
    reason: str
    username: str | None = None

    def detail(self) -> str:
        return f"login {self.status}: {self.reason}"


@dataclass
class RunReport:
    """Per-run flags by host (lockout, wrong_password, captcha, ...). No secrets, ever."""
    flags: dict[str, list[str]] = field(default_factory=dict)

    def flag(self, host: str, what: str) -> None:
        self.flags.setdefault(host, [])
        if what not in self.flags[host]:
            self.flags[host].append(what)

    def compact(self) -> dict[str, list[str]]:
        return {h: list(v) for h, v in self.flags.items()}


# Email verification for a new account through the unattended mail path (a later component registers it).
EmailVerifier = Callable[[str, str], Awaitable[bool]]     # (host, email) -> verified
_email_verifier: EmailVerifier | None = None


def register_email_verifier(fn: EmailVerifier | None) -> None:
    global _email_verifier
    _email_verifier = fn


_TERMS_TEXT = re.compile(r"terms|privacy|policy|data (protection|processing)|gdpr", re.IGNORECASE)
# Consents the owner did NOT delegate, even when the box also mentions terms or privacy (ADR-0046).
_NOT_TERMS = re.compile(r"sms|text messag|whatsapp|\bcall|arbitrat|background|drug|certif|attest|"
                        r"true and (complete|accurate)|marketing", re.IGNORECASE)
_OPTIONAL_TEXT = re.compile(r"newsletter|marketing|job alert|notify me|send me|promotional|talent (community|pool)|"
                            r"similar (jobs|roles)|future opportunit", re.IGNORECASE)


def _stop_hit(text: str, phrases: list[str]) -> str | None:
    low = text.lower()
    return next((p for p in phrases if p.strip() and p.lower() in low), None)


_MOVED = "login form moved to another host; not filling or submitting"


def _on_host(ops: LoginOps, host: str) -> bool:
    """The page must still be on the exact host whose credential is in use (redirects and links can leave it)."""
    try:
        return normalize_host(ops.url()) == host
    except VaultError:
        return False


class AccountManager:
    def __init__(self, settings: Any, vault: Vault, db: AccountsDB, report: RunReport | None = None):
        self.s = settings
        self.cfg = settings.accounts
        self.vault = vault
        self.db = db
        self.report = report if report is not None else RunReport()
        self._attempts: dict[str, int] = {}
        self._stopped: dict[str, str] = {}

    def attempts(self, host: str) -> int:
        return self._attempts.get(host, 0)

    def _stop(self, host: str, why: str) -> LoginOutcome:
        self._stopped[host] = why
        self.report.flag(host, why)
        log.warning("accounts: %s stopped for this run (%s)", host, why)
        return LoginOutcome(host, "blocked", why)

    async def handle_wall(self, ops: LoginOps, url: str, *, email: str | None = None,
                          dry_run: bool = True) -> LoginOutcome:
        try:
            host = normalize_host(url)
        except VaultError as e:
            return LoginOutcome("?", "needs_human", str(e))
        if host in self._stopped:
            return LoginOutcome(host, "blocked", f"stopped earlier this run ({self._stopped[host]})")
        if not consent.active(self.s, "credentials"):
            return LoginOutcome(host, "skipped", "no active consent window for credentials")
        if await ops.captcha_visible():
            self.report.flag(host, "captcha")
            return LoginOutcome(host, "needs_human", "visible CAPTCHA: human handoff (never solved)")
        try:
            users = self.vault.usernames(host)
        except VaultError as e:
            return LoginOutcome(host, "needs_human", str(e))
        if users:
            return await self._login(ops, host, users, email)
        return await self._create(ops, host, email, dry_run)

    # -- login ----------------------------------------------------------------------------------
    def _pick(self, users: list[str], email: str | None) -> str | None:
        if len(users) == 1:
            return users[0]
        want = {x.lower() for x in (self.cfg.signup_email, email or "") if x}
        hit = [u for u in users if u.lower() in want]
        return hit[0] if len(hit) == 1 else None

    async def _login(self, ops: LoginOps, host: str, users: list[str], email: str | None) -> LoginOutcome:
        user = self._pick(users, email)
        if user is None:
            return LoginOutcome(host, "needs_human", f"{len(users)} vault entries for {host}; cannot choose one")
        while self.attempts(host) < self.cfg.max_login_attempts:
            self._attempts[host] = self.attempts(host) + 1
            try:
                secret = self.vault.get(host, user)       # ONLY the credential stored for this exact host
            except VaultError as e:
                return LoginOutcome(host, "needs_human", str(e), user)
            if secret is None:
                return LoginOutcome(host, "needs_human", f"vault index lists {host} but the keychain has no item", user)
            if await ops.kind() != "login" and not await ops.open_signin():
                return LoginOutcome(host, "needs_human", "could not find the sign-in form", user)
            if await ops.captcha_visible():
                self.report.flag(host, "captcha")
                return LoginOutcome(host, "needs_human", "visible CAPTCHA: human handoff (never solved)", user)
            if not _on_host(ops, host):
                self.report.flag(host, "host_moved")
                return LoginOutcome(host, "needs_human", _MOVED, user)
            if not await ops.fill_username(user):
                return LoginOutcome(host, "needs_human", "sign-in fields not fillable", user)
            if not _on_host(ops, host):
                self.report.flag(host, "host_moved")
                return LoginOutcome(host, "needs_human", _MOVED, user)
            if not await ops.fill_password(secret):
                return LoginOutcome(host, "needs_human", "sign-in fields not fillable", user)
            del secret
            if await ops.captcha_visible():
                self.report.flag(host, "captcha")
                return LoginOutcome(host, "needs_human", "visible CAPTCHA before sign-in submit: human handoff", user)
            if not _on_host(ops, host):
                self.report.flag(host, "host_moved")
                return LoginOutcome(host, "needs_human", _MOVED, user)
            if not await ops.submit("login"):
                return LoginOutcome(host, "needs_human", "sign-in button not found", user)
            res = await ops.login_result()
            if res == "ok":
                return LoginOutcome(host, "logged_in", f"signed in (attempt {self.attempts(host)})", user)
            if res in ("locked", "wrong_password"):
                return self._stop(host, "lockout" if res == "locked" else "wrong_password")
            if res == "captcha":
                self.report.flag(host, "captcha")
                return LoginOutcome(host, "needs_human", "CAPTCHA after sign-in: human handoff", user)
            if res == "verification":
                self.report.flag(host, "verification_code")
                return LoginOutcome(host, "needs_human", "site asks for a verification code: human only", user)
        return self._stop(host, "login_failed")

    # -- account creation -------------------------------------------------------------------------
    async def _create(self, ops: LoginOps, host: str, email: str | None, dry_run: bool) -> LoginOutcome:
        if not self.cfg.auto_create:
            return LoginOutcome(host, "needs_human", "no vault entry for this host (accounts.auto_create is off)")
        user = (self.cfg.signup_email or email or "").strip()
        if not user:
            return LoginOutcome(host, "needs_human", "no email for a new account")
        if dry_run:
            return LoginOutcome(host, "needs_human", "dry_run: an account would be created here", user)
        if self.attempts(host) >= 1:
            return LoginOutcome(host, "blocked", "one account creation per host per run", user)
        self._attempts[host] = self.attempts(host) + 1
        if await ops.kind() != "signup" and not await ops.open_signup():
            return LoginOutcome(host, "needs_human", "could not find the create-account form", user)
        if not _on_host(ops, host):
            self.report.flag(host, "host_moved")
            return LoginOutcome(host, "needs_human", _MOVED, user)
        if await ops.captcha_visible():
            self.report.flag(host, "captcha")
            return LoginOutcome(host, "needs_human", "visible CAPTCHA on sign-up: human handoff", user)
        to_accept: list[Terms] = []
        for t in await ops.terms():
            if not t.required:
                continue                                   # optional boxes (alerts, marketing) stay unticked
            if (hit := _stop_hit(t.text, self.s.policy.stop_phrases)):
                return LoginOutcome(host, "needs_human", f"sign-up attestation: '{hit}'", user)
            if not _TERMS_TEXT.search(t.text) or _NOT_TERMS.search(t.text) or _OPTIONAL_TEXT.search(t.text):
                return LoginOutcome(host, "needs_human", f"required sign-up checkbox is not a terms box: "
                                                         f"{t.text[:80]!r}", user)
            to_accept.append(t)
        terms_url = next((t.url for t in to_accept if t.url), None) or (ops.url() if to_accept else None)

        secret = generate_password(self.cfg.password_length)
        try:
            self.vault.put(host, user, secret)             # keychain FIRST: a crash after submit cannot lose it
        except VaultError as e:
            return LoginOutcome(host, "needs_human", f"not creating an account: {e}", user)
        self.db.record(host, user, "creating", terms_url)
        ok = await ops.fill_username(user)
        moved = not _on_host(ops, host)
        ok = ok and not moved and await ops.fill_password(secret)
        moved = moved or not _on_host(ops, host)
        ok = ok and not moved and await ops.fill_password(secret, confirm=True)
        del secret
        if moved:
            self.db.record(host, user, "failed")
            self.vault.remove(host, user)
            self.report.flag(host, "host_moved")
            return LoginOutcome(host, "needs_human", _MOVED, user)
        for t in to_accept:
            ok = ok and await ops.accept_terms(t)
        if not ok:
            self.db.record(host, user, "failed")
            self.vault.remove(host, user)
            return LoginOutcome(host, "needs_human", "sign-up fields not fillable; nothing submitted", user)
        if await ops.captcha_visible():
            self.db.record(host, user, "failed")
            self.vault.remove(host, user)
            self.report.flag(host, "captcha")
            return LoginOutcome(host, "needs_human", "visible CAPTCHA on sign-up: human handoff", user)
        if not _on_host(ops, host):
            self.db.record(host, user, "failed")
            self.vault.remove(host, user)
            self.report.flag(host, "host_moved")
            return LoginOutcome(host, "needs_human", _MOVED, user)
        if not await ops.submit("signup"):
            self.db.record(host, user, "failed")
            self.vault.remove(host, user)
            return LoginOutcome(host, "needs_human", "create-account button not found; nothing submitted", user)
        res = await ops.signup_result()
        self.report.flag(host, "account_created_terms_to_review" if to_accept else "account_created")
        if res == "created":
            self.db.record(host, user, "active")
            return LoginOutcome(host, "created", "account created; password stored in the keychain", user)
        if res == "verify_email":
            if consent.active(self.s, "mail") and _email_verifier is not None:
                try:
                    verified = await _email_verifier(host, user)
                except Exception as e:  # noqa: BLE001
                    log.warning("accounts: email verifier failed for %s (%s)", host, type(e).__name__)
                    verified = False
                if verified:
                    self.db.record(host, user, "active")
                    return LoginOutcome(host, "created", "account created and email verified", user)
            self.db.record(host, user, "pending_verification")
            self.report.flag(host, "pending_verification")
            return LoginOutcome(host, "pending_verification", "account created; confirm the email the site sent",
                                user)
        if res == "exists":
            self.db.record(host, user, "failed")
            self.vault.remove(host, user)
            self.report.flag(host, "account_exists")
            return LoginOutcome(host, "needs_human", "an account already exists: add it with `job-apply-mcp "
                                                     "vault add`", user)
        if res == "captcha":
            self.db.record(host, user, "unknown")
            self.report.flag(host, "captcha")
            return LoginOutcome(host, "needs_human", "CAPTCHA after sign-up: human handoff", user)
        self.db.record(host, user, "unknown")
        self.report.flag(host, "signup_unknown")
        return LoginOutcome(host, "needs_human", "sign-up result unclear; the keychain entry was kept", user)

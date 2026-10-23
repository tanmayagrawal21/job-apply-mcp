"""Consent window (ADR-0048): the human consents ONCE per period (one run, a day, a week) from a terminal; inside
the window unattended actions need no human, and the user reviews afterwards.

The interface for other modules is deliberately small:

    consent.active(settings, scope)          -> Consent | None   read-only: is `scope` covered right now?
    consent.use(settings, scope, detail="")  -> Consent | None   record one unattended action (audited; binds a
                                                                  --per-run consent to this run; approve_submissions
                                                                  consumes one of --max)
    with consent.run_context(run_id): ...    optional: a --per-run consent covers exactly this run
    consent.end_run(settings)                optional: close this run's --per-run consent early

Scopes: approve_submissions, captcha_attempt, otp_unattended (alias `mail`, used by sign-up email verification),
credentials (reserved). Nothing here mints a consent;
only `job-apply-mcp consent` in an interactive terminal does.
"""
from __future__ import annotations

from typing import Any

from ..grants.store import GrantError
from .store import CONSENT_SCOPES, DEFAULT_SCOPES, Consent, ConsentStore, current_run, run_context, store_for

__all__ = ["CONSENT_SCOPES", "DEFAULT_SCOPES", "Consent", "ConsentStore", "active", "current_run", "end_run",
           "run_context", "status", "store_for", "use"]


# Accepted spellings from sibling modules; each maps to exactly one real scope.
SCOPE_ALIASES = {"mail": "otp_unattended", "otp": "otp_unattended"}


def _scope(scope: str) -> str:
    return SCOPE_ALIASES.get(scope, scope)


def active(settings: Any, scope: str) -> Consent | None:
    """Read-only. The consent covering `scope` for this run, or None. Raises ValueError for an unknown scope."""
    scope = _scope(scope)
    if scope not in CONSENT_SCOPES:
        raise ValueError(f"unknown consent scope {scope!r}; known: {sorted(CONSENT_SCOPES)}")
    try:
        return store_for(settings).use(scope, consume=False)
    except GrantError:
        return None


def use(settings: Any, scope: str, detail: str = "") -> Consent | None:
    """Record one unattended action under `scope`, or None when no consent covers it (caller keeps today's
    human path). NEVER pass a verification code or other secret in `detail`."""
    scope = _scope(scope)
    if scope not in CONSENT_SCOPES:
        raise ValueError(f"unknown consent scope {scope!r}; known: {sorted(CONSENT_SCOPES)}")
    try:
        return store_for(settings).use(scope, detail=detail)
    except GrantError:
        return None


def end_run(settings: Any) -> list[str]:
    return store_for(settings).end_run()


def status(settings: Any) -> dict[str, Any]:
    out: dict[str, Any] = {"active": store_for(settings).status(), "scopes": sorted(CONSENT_SCOPES)}
    if not any(i.strip() for i in settings.verification.trusted_authserv_ids):
        from .otp import OTP_SETUP_HINT
        out["otp_unattended"] = f"off: {OTP_SETUP_HINT}"
    elif not getattr(settings, "_otp_mail_trusted", False):
        out["otp_unattended"] = "off: mail settings must come from the trusted user config"
    return out

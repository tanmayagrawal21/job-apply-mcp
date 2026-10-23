"""Consent windows: `active(settings, scope) -> bool` (ADR-0046 stub).

The real implementation (human-opened, time-limited consent windows) lands separately on feat/consent-window and
replaces this module. Until then every scope is closed, so nothing gated on consent (credential use, account
creation, unattended mail) can run. Callers must read the gate through this module attribute
(`consent.active(...)`), never a copied reference, so the replacement takes effect everywhere.
"""
from __future__ import annotations

from typing import Any

SCOPES = ("credentials", "mail")


def active(settings: Any, scope: str) -> bool:
    """True only while a human-opened consent window for `scope` is open. Stub: always False (fail closed)."""
    return False

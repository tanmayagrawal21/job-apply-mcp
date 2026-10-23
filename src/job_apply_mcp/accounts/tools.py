"""MCP wiring: read-only `vault_status` (ADR-0046). No tool reads, adds or removes a secret."""
from __future__ import annotations

import json
from collections.abc import Callable
from typing import Any

from .. import consent
from .runtime import RUN_REPORT
from .store import AccountsDB
from .vault import Vault, VaultError


def status(settings: Any, vault: Vault | None = None, db: AccountsDB | None = None) -> dict[str, Any]:
    v = vault or Vault.from_settings(settings)
    try:
        hosts: dict[str, int] = {}
        for e in v.entries():
            hosts[e["host"]] = hosts.get(e["host"], 0) + 1
        err = None
    except VaultError as e:
        hosts, err = {}, str(e)
    d = db or AccountsDB(settings.paths.data_dir)
    try:
        pending = [a["host"] for a in d.list("pending_verification")]
    finally:
        if db is None:
            d.close()
    out: dict[str, Any] = {
        "consent": consent.active(settings, "credentials") is not None,   # ADR-0048 returns the Consent or None
        "auto_create": settings.accounts.auto_create,
        "hosts": [{"host": h, "logins": n} for h, n in sorted(hosts.items())],
        "pending_verification": pending,
        "run_flags": RUN_REPORT.compact(),
    }
    if err:
        out["error"] = err
    return out


def register(mcp: Any, get_settings: Callable[[], Any]) -> None:
    @mcp.tool()
    async def vault_status() -> str:
        """Which ATS hosts have a stored login (host and count only, never a username's password), whether the
        credentials consent window is open, accounts awaiting email verification, and this run's login flags
        (lockout, wrong_password, captcha). Read-only: logins are added by a human with `job-apply-mcp vault add`."""
        return json.dumps(status(get_settings()), separators=(",", ":"))

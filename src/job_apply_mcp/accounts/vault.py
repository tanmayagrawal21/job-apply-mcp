"""Credential vault over the OS keychain (ADR-0046). Optional extra: `keyring`.

Each secret is one keychain item: service `job-apply-mcp-vault:<host>`, account `<username>`. Keychains cannot be
enumerated portably, so a small index of (host, username, added_at) lives in `<data_dir>/vault_index.json` (0600).
The index holds NO secret. There is no file fallback for secrets: without a real keychain backend the vault
refuses to store anything.
"""
from __future__ import annotations

import json
import logging
import os
import re
import time
from pathlib import Path
from typing import Any, Protocol
from urllib.parse import urlsplit

from ..core.secrets import Secret, forget

log = logging.getLogger(__name__)
SERVICE = "job-apply-mcp-vault"
_HOST = re.compile(r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?(?:\.[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?)+")
# keyring backends that do not protect secrets at rest (fail = none, plaintext/null from keyrings.alt)
_INSECURE = re.compile(r"\bfail\b|plaintext|\bnull\b|keyrings\.alt", re.IGNORECASE)


class VaultError(RuntimeError):
    """Never carries a secret: messages are built from hosts, usernames and type names only."""


class Backend(Protocol):
    def get_password(self, service: str, username: str) -> str | None: ...
    def set_password(self, service: str, username: str, password: str) -> None: ...
    def delete_password(self, service: str, username: str) -> None: ...


def normalize_host(host_or_url: str) -> str:
    """Exact lowercase hostname (no scheme, port, path or trailing dot). Credentials are looked up for this exact
    host only: no parent-domain or wildcard fallback, so a tenant's login is never offered to another tenant."""
    s = (host_or_url or "").strip()
    h = (urlsplit(s).hostname or "") if "://" in s else s.split("/")[0].split(":")[0]
    h = h.lower().rstrip(".")
    if not _HOST.fullmatch(h):
        raise VaultError(f"not a valid hostname: {h[:80]!r}")
    return h


def _validate_username(u: str) -> str:
    u = (u or "").strip()
    if not u or len(u) > 254 or any(c in u for c in "\r\n\t\x00"):
        raise VaultError("username must be 1-254 printable characters")
    return u


def default_backend() -> Backend:
    try:
        import keyring  # lazy: optional extra
    except ImportError:
        raise VaultError("the vault needs the OS keychain: install the 'keyring' extra "
                         "(uv sync --extra keyring)") from None
    kr = keyring.get_keyring()
    for b in [kr, *getattr(kr, "backends", [])]:     # ChainerBackend delegates: check every member
        name = f"{type(b).__module__}.{type(b).__name__}"
        if _INSECURE.search(name):
            raise VaultError(f"keyring backend {name} does not protect secrets at rest; refusing to use it")
    return kr


class Vault:
    def __init__(self, data_dir: str | Path, backend: Backend | None = None):
        self._backend = backend
        self.index_path = Path(data_dir).expanduser() / "vault_index.json"

    @classmethod
    def from_settings(cls, settings: Any, backend: Backend | None = None) -> Vault:
        return cls(settings.paths.data_dir, backend)

    @property
    def backend(self) -> Backend:
        if self._backend is None:
            self._backend = default_backend()
        return self._backend

    # -- index (no secrets) ---------------------------------------------------------------------
    def _read_index(self) -> list[dict[str, Any]]:
        try:
            rows = json.loads(self.index_path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            return []
        except (OSError, ValueError) as e:
            raise VaultError(f"vault index unreadable ({type(e).__name__})") from None
        return [r for r in rows if isinstance(r, dict) and {"host", "username"} <= r.keys()]

    def _write_index(self, rows: list[dict[str, Any]]) -> None:
        self.index_path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.index_path.with_suffix(".tmp")
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(sorted(rows, key=lambda r: (r["host"], r["username"])), f, indent=1)
        os.replace(tmp, self.index_path)

    def entries(self) -> list[dict[str, Any]]:
        """[(host, username, added_at)] only."""
        return [{"host": r["host"], "username": r["username"], "added_at": r.get("added_at", "")}
                for r in self._read_index()]

    def usernames(self, host: str) -> list[str]:
        h = normalize_host(host)
        return [r["username"] for r in self._read_index() if r["host"] == h]

    # -- secrets ----------------------------------------------------------------------------------
    @staticmethod
    def service(host: str) -> str:
        return f"{SERVICE}:{normalize_host(host)}"

    def put(self, host: str, username: str, secret: Secret) -> None:
        if not isinstance(secret, Secret):
            raise VaultError("put() takes a Secret")
        h, u = normalize_host(host), _validate_username(username)
        try:
            self.backend.set_password(self.service(h), u, secret.reveal())
        except VaultError:
            raise
        except Exception as e:  # noqa: BLE001 - backend errors may echo arguments; keep only the type
            err = type(e).__name__
        else:
            err = None
        if err:                       # raised outside the except block: no chained exception that could hold it
            raise VaultError(f"keychain refused the entry for {h} ({err})")
        rows = [r for r in self._read_index() if not (r["host"] == h and r["username"] == u)]
        rows.append({"host": h, "username": u, "added_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())})
        self._write_index(rows)

    def get(self, host: str, username: str) -> Secret | None:
        """The stored secret for exactly (host, username), or None."""
        h, u = normalize_host(host), _validate_username(username)
        try:
            v = self.backend.get_password(self.service(h), u)
        except VaultError:
            raise
        except Exception as e:  # noqa: BLE001
            err = type(e).__name__
        else:
            err = None
        if err:
            raise VaultError(f"keychain read failed for {h} ({err})")
        return Secret(v) if v else None

    def remove(self, host: str, username: str | None = None) -> list[str]:
        """Delete the keychain item(s) and index rows for host (one username or all). Returns removed usernames."""
        h = normalize_host(host)
        gone = []
        for u in self.usernames(h):
            if username is not None and u != username:
                continue
            try:
                old = self.backend.get_password(self.service(h), u)
                if old:
                    forget(old)
                self.backend.delete_password(self.service(h), u)
            except Exception as e:  # noqa: BLE001 - already gone is fine; never surface backend text
                log.debug("keychain delete for %s: %s", h, type(e).__name__)
            gone.append(u)
        if gone:
            self._write_index([r for r in self._read_index() if not (r["host"] == h and r["username"] in gone)])
        return gone

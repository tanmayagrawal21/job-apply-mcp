"""Process-wide ("per run") account state (ADR-0046): one RunReport and one AccountManager per server process,
so the 2-attempts-per-host limit and stopped hosts hold across every job of the run."""
from __future__ import annotations

import threading
from typing import Any

from .login import AccountManager, RunReport
from .store import AccountsDB
from .vault import Vault

RUN_REPORT = RunReport()
_lock = threading.Lock()
_manager: AccountManager | None = None


def manager_for(settings: Any) -> AccountManager:
    global _manager
    with _lock:
        if _manager is None:
            _manager = AccountManager(settings, Vault.from_settings(settings), AccountsDB(settings.paths.data_dir),
                                      RUN_REPORT)
        return _manager


def reset() -> None:
    """Tests only."""
    global _manager
    with _lock:
        if _manager is not None:
            _manager.db.close()
        _manager = None
        RUN_REPORT.flags.clear()

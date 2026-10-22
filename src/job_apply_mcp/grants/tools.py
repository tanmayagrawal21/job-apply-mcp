"""MCP wiring: read-only `grants_status`. There is deliberately no tool that creates, extends or revokes a grant."""
from __future__ import annotations

import json
import time
from collections.abc import Callable
from typing import Any

from .store import store_for


def status(settings) -> dict[str, Any]:
    now = time.time()
    return {"active": [{"id": g.id, "scope": g.scope, "expires_in_s": int(g.expires - now),
                        "uses_left": left} for g, left in store_for(settings).active()]}


def register(mcp: Any, get_settings: Callable[[], Any]) -> None:
    @mcp.tool()
    async def grants_status() -> str:
        """Active human-issued grants (scope, seconds left, uses left). Read-only: only a human can create or
        revoke a grant, via `job-apply-mcp grant` in a terminal."""
        return json.dumps(status(get_settings()), separators=(",", ":"))

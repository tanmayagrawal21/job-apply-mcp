"""MCP wiring: read-only `consent_status`. There is deliberately no tool that mints, extends or revokes a consent."""
from __future__ import annotations

import json
from collections.abc import Callable
from typing import Any

from . import status


def register(mcp: Any, get_settings: Callable[[], Any]) -> None:
    @mcp.tool()
    async def consent_status() -> str:
        """Active consent windows (mode, scopes, seconds left, submissions left). Read-only: only a human can
        consent, in a terminal: `job-apply-mcp consent --for 24h` or `--per-run`."""
        return json.dumps(status(get_settings()), separators=(",", ":"))

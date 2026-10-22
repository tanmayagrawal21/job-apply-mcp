"""MCP tools for the control layer. `register(mcp, get_settings)` is the only wiring server.py needs."""
from __future__ import annotations

import json
import logging
from collections.abc import Callable
from typing import Any

from ..config import Settings
from .agent import ControlAgent, Reviewer

log = logging.getLogger(__name__)
_agent: ControlAgent | None = None
_reviewer: Reviewer | None = None


def set_reviewer(reviewer: Reviewer | None) -> None:
    """Called by the LLM wiring (router task_class 'review') once available."""
    global _agent, _reviewer
    _reviewer, _agent = reviewer, None


def get_agent(settings: Settings) -> ControlAgent:
    global _agent
    if _agent is None:
        _agent = ControlAgent(settings, reviewer=_reviewer)
    return _agent


def register(mcp: Any, get_settings: Callable[[], Settings]) -> None:
    @mcp.tool()
    async def control_check(action: str, payload: dict[str, Any] | None = None) -> str:
        """Check a side-effecting action against the control layer. action: apply|submit|send_message|
        write_prose|merge. payload: ref, platform, company, dry_run, text, recipient, page_text, form_labels,
        attestations. Returns {decision: pass|block|needs_human, violations, warnings}."""
        try:
            rep = await get_agent(get_settings()).check(action, payload or {})
            out = rep.compact()
        except ValueError as e:
            out = {"decision": "block", "error": str(e)}
        return json.dumps(out, separators=(",", ":"), default=str)

    @mcp.tool()
    async def control_status() -> str:
        """Active mode, quotas used/remaining, enabled controls and recent blocks."""
        settings = get_settings()
        st = get_agent(settings).status()
        try:  # ADR-0025: confirmation checks still pending (counts only)
            from ..apply.approvals import ApprovalBook
            from ..ingest.reconcile import pending_summary

            book = ApprovalBook(settings.paths.data_dir, reconcile_delays_min=settings.ingest.reconcile_delays_min)
            summary = pending_summary(book)
            book._db.close()
            if summary["open"]:
                st["pending_reconciliations"] = summary
        except Exception as e:  # noqa: BLE001 - status must never fail on an unwritable data dir
            log.debug("reconciliation summary unavailable: %s", e)
        return json.dumps(st, separators=(",", ":"), default=str)

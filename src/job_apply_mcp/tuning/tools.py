"""MCP tools: tune_status, tune_run. `register(mcp, get_settings)` is the only wiring server.py needs."""
from __future__ import annotations

import asyncio
import json
from collections.abc import Callable
from pathlib import Path
from typing import Any

from ..config import Settings
from .manager import TuneManager

_mgr: TuneManager | None = None
_source: Any = None
_tasks: set[asyncio.Task] = set()


def set_source(source: Any) -> None:
    """Inject the DatasetSource (ProvenanceDatasetSource once ADR-0007 is wired); resets the manager."""
    global _source, _mgr
    _source, _mgr = source, None


def _default_source(settings: Settings) -> Any:
    from ..provenance import ProvenanceDatasetSource, ProvenanceStore

    return ProvenanceDatasetSource(ProvenanceStore(settings.provenance.db_path
                                                   or Path(settings.paths.data_dir).expanduser() / "provenance.db"))


def get_manager(settings: Settings) -> TuneManager:
    global _mgr
    if _mgr is None:
        _mgr = TuneManager(settings, _source or _default_source(settings))
    return _mgr


def _j(o: Any) -> str:
    return json.dumps(o, separators=(",", ":"), default=str)


def register(mcp: Any, get_settings: Callable[[], Settings]) -> None:
    @mcp.tool()
    async def tune_status() -> str:
        """Local style-tuning state: backend, promoted/previous adapter, new user texts vs threshold, last run."""
        m = get_manager(get_settings())
        out = await asyncio.to_thread(m.status)
        if m.cfg.auto_run and not out["running"] and out["new_texts"] >= m.cfg.new_data_threshold:
            t = asyncio.create_task(asyncio.to_thread(m.run, "new_data"))
            _tasks.add(t)
            t.add_done_callback(_tasks.discard)
            out["auto_started"] = True
        return _j(out)

    @mcp.tool()
    async def tune_run(trigger: str = "manual", wait: bool = False) -> str:
        """Train a LoRA on the user's own texts, gate it against the incumbent, promote only if better.
        trigger: manual | new_data (skips below the configured threshold) | rollback (restore previous adapter).
        wait=False starts in the background; poll tune_status."""
        m = get_manager(get_settings())
        if trigger == "rollback":
            return _j(await asyncio.to_thread(m.rollback))
        if wait:
            return _j(await asyncio.to_thread(m.run, trigger))
        t = asyncio.create_task(asyncio.to_thread(m.run, trigger))
        _tasks.add(t)
        t.add_done_callback(_tasks.discard)
        return _j({"status": "started", "trigger": trigger})

"""Service layer: wires registries + config into the operations the tools expose.

Component PRs fill in the TODOs; each method must stay usable (raising NotImplementedError with a clear
message) until its component lands, so the server always starts.
"""
from __future__ import annotations

from typing import Any

from .config import Settings
from .core.concurrency import HostLimiter, bounded_gather


class Services:
    def __init__(self, settings: Settings):
        self.settings = settings
        c = settings.concurrency
        self.hosts = HostLimiter(per_host=c.per_host, min_interval_s=c.min_interval_s)

    async def search(self, **kw) -> dict[str, Any]:
        raise NotImplementedError("platform adapters land in feat/platform-adapters")

    async def get_job(self, ref_or_url: str, fields: list[str] | None = None) -> dict[str, Any]:
        raise NotImplementedError("platform adapters land in feat/platform-adapters")

    async def get_form(self, ref_or_url: str) -> dict[str, Any]:
        raise NotImplementedError("platform adapters land in feat/platform-adapters")

    async def plan(self, ref_or_url: str) -> dict[str, Any]:
        raise NotImplementedError("apply engine lands in feat/apply-engine")

    async def submit(self, ref_or_url: str, answers: dict[str, Any], dry_run: bool | None) -> dict[str, Any]:
        raise NotImplementedError("apply engine lands in feat/apply-engine")

    async def apply_batch(self, refs: list[str], dry_run: bool | None, max_parallel: int | None) -> dict[str, Any]:
        limit = max_parallel or self.settings.concurrency.browser_workers
        rows = await bounded_gather((self.submit(r, {}, dry_run) for r in refs), limit)
        return {"results": [r if not isinstance(r, BaseException) else {"error": str(r)} for r in rows]}

    async def list_applications(self, status: str, limit: int, cursor: int) -> dict[str, Any]:
        raise NotImplementedError("store lands in feat/store-policy")

    async def health(self) -> dict[str, Any]:
        from .core import registry

        return {"platforms": registry.platforms.names(), "llm": registry.llm_providers.names(),
                "browser": registry.browser_drivers.names(), "dry_run": self.settings.dry_run}

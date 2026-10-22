"""Service layer: wires registries + config into the operations the tools expose.

Component PRs fill in the TODOs; each method must stay usable (raising NotImplementedError with a clear
message) until its component lands, so the server always starts.
"""
from __future__ import annotations

from typing import Any

from .config import Settings
from .core.concurrency import HostLimiter


class Services:
    def __init__(self, settings: Settings, engine: Any = None):
        self.settings = settings
        self._engine = engine
        c = settings.concurrency
        self.hosts = HostLimiter(per_host=c.per_host, min_interval_s=c.min_interval_s)
        self._store = None

    @property
    def store(self):
        """SQLite store, opened lazily so the server starts even if the data dir is unwritable."""
        if self._store is None:
            from .store.sqlite import SqliteStore

            self._store = SqliteStore(self.settings.paths.data_dir, dedupe_days=self.settings.policy.dedupe_days)
        return self._store

    @property
    def engine(self):
        """Apply engine, built lazily (ADR-0011); raises ComponentMissing with a clear message if a
        required sibling component is not installed."""
        if self._engine is None:
            from .apply.factory import build_engine

            self._engine = build_engine(self.settings)
        return self._engine

    async def search(self, **kw) -> dict[str, Any]:
        raise NotImplementedError("platform adapters land in feat/platform-adapters")

    async def get_job(self, ref_or_url: str, fields: list[str] | None = None) -> dict[str, Any]:
        raise NotImplementedError("platform adapters land in feat/platform-adapters")

    async def get_form(self, ref_or_url: str) -> dict[str, Any]:
        raise NotImplementedError("platform adapters land in feat/platform-adapters")

    async def plan(self, ref_or_url: str) -> dict[str, Any]:
        return (await self.engine.plan(ref_or_url)).compact()

    async def submit(self, ref_or_url: str, answers: dict[str, Any], dry_run: bool | None,
                     token: str | None = None) -> dict[str, Any]:
        r = await self.engine.submit(ref_or_url, answers, token, dry_run)
        row = {"ref": r.ref, "status": r.status, "via": r.via, "confirmation": r.confirmation, "detail": r.detail,
               "t": self.engine.timings.get(ref_or_url)}
        return {k: v for k, v in row.items() if v}

    async def apply_batch(self, refs: list[str], dry_run: bool | None, max_parallel: int | None) -> dict[str, Any]:
        return {"results": await self.engine.apply_batch(refs, dry_run, max_parallel)}

    async def list_applications(self, status: str, limit: int, cursor: int) -> dict[str, Any]:
        page = await self.store.list(status="" if status in ("", "all") else status, limit=limit, cursor=cursor)
        rows = [{"ref": r["ref"], "co": r["company"], "title": r["title"], "st": r["status"],
                 "at": r["created_at"][:10]} for r in page["rows"]]
        return {"rows": rows, "next": page["next"], "total": page["total"]}

    async def health(self) -> dict[str, Any]:
        from .core import registry

        return {"platforms": registry.platforms.names(), "llm": registry.llm_providers.names(),
                "browser": registry.browser_drivers.names(), "dry_run": self.settings.dry_run}

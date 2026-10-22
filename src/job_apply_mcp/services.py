"""Service layer: wires registries + config into the operations the tools expose.

Component PRs fill in the TODOs; each method must stay usable (raising NotImplementedError with a clear
message) until its component lands, so the server always starts.
"""
from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

from .config import Settings
from .core.concurrency import HostLimiter, bounded_gather


class Services:
    def __init__(self, settings: Settings):
        self.settings = settings
        c = settings.concurrency
        self._prov = None
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

    # --- provenance + tuning (feat/provenance) ---
    @property
    def prov(self):
        if self._prov is None:
            from .provenance import ProvenanceStore

            cfg, data = self.settings.provenance, Path(self.settings.paths.data_dir).expanduser()
            self._prov = ProvenanceStore(cfg.db_path or data / "provenance.db")
        return self._prov

    async def record_text(self, kind: str, text: str, origin: str, parent: str | None = None,
                          model: str | None = None, prompt: str | None = None,
                          context: str | None = None) -> dict[str, Any]:
        rec = await asyncio.to_thread(self.prov.record_text, kind, text, origin, parent=parent, model=model,
                                      prompt=prompt, context=context)
        return rec.compact()

    async def verify_text(self, id: str, edited_text: str | None = None, verified_by: str = "user",
                          ) -> dict[str, Any]:
        rec = await asyncio.to_thread(self.prov.verify_text, id, edited_text=edited_text, verified_by=verified_by)
        return {**rec.compact(), **({"diff": rec.diff_from_parent} if rec.diff_from_parent else {})}

    async def provenance(self, id: str, include_text: bool = False) -> dict[str, Any]:
        chain = await asyncio.to_thread(self.prov.lineage, id)
        if not chain:
            return {"error": f"unknown record {id}"}
        rows = [r.compact() for r in chain]
        if include_text:
            rows[0]["text"] = chain[0].text
        return {"record": rows[0], "ancestors": rows[1:],
                "llm_generated": chain[0].llm_generated, **({"diff": chain[0].diff_from_parent}
                                                           if chain[0].diff_from_parent else {})}

    async def export_provenance(self, ids: list[str], target: str | None = None,
                                mode: str | None = None) -> dict[str, Any]:
        from .provenance import tag_export

        return await asyncio.to_thread(tag_export, self.prov, ids, mode or self.settings.provenance.export_tagging,
                                       target)

    async def health(self) -> dict[str, Any]:
        from .core import registry

        return {"platforms": registry.platforms.names(), "llm": registry.llm_providers.names(),
                "browser": registry.browser_drivers.names(), "dry_run": self.settings.dry_run}

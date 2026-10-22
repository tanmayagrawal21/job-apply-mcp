"""Service layer: wires registries + config into the operations the tools expose.

Component PRs fill in the TODOs; each method must stay usable (raising NotImplementedError with a clear
message) until its component lands, so the server always starts.
"""
from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from .config import Settings
from .core.concurrency import HostLimiter, bounded_gather
from .core.interfaces import PlatformAdapter
from .core.models import Posting
from .platforms import AdapterError, HttpClient, build_adapters, detect
from .platforms.textflags import grad_window_conflict, years_required


class Services:
    def __init__(self, settings: Settings):
        self.settings = settings
        c = settings.concurrency
        self.hosts = HostLimiter(per_host=c.per_host, min_interval_s=c.min_interval_s)
        self._http: HttpClient | None = None
        self._adapters: dict[str, PlatformAdapter] | None = None
        self._store = None

    @property
    def store(self):
        """SQLite store, opened lazily so the server starts even if the data dir is unwritable."""
        if self._store is None:
            from .store.sqlite import SqliteStore

            self._store = SqliteStore(self.settings.paths.data_dir, dedupe_days=self.settings.policy.dedupe_days)
        return self._store

    # ------------------------------------------------------------------ platform plumbing
    @property
    def http(self) -> HttpClient:
        if self._http is None:
            p = self.settings.platforms
            self._http = HttpClient(
                cache_dir=Path(self.settings.paths.data_dir).expanduser() / "http_cache", ttl_s=p.cache_ttl_s,
                limiter=self.hosts, timeout_s=p.timeout_s, retries=p.retries, backoff_s=p.backoff_s,
                user_agent=p.user_agent, prune_days=p.cache_prune_days)
        return self._http

    @property
    def adapters(self) -> dict[str, PlatformAdapter]:
        if self._adapters is None:
            self._adapters = build_adapters(self.settings.platforms_enabled, self.http, self.settings.platforms)
        return self._adapters

    async def aclose(self) -> None:
        if self._http:
            await self._http.aclose()

    def resolve(self, ref_or_url: str) -> PlatformAdapter:
        ads = self.adapters
        head = ref_or_url.split(":", 1)[0]
        if head in ads and not ref_or_url.startswith(("http:", "https:")):
            return ads[head]
        if a := detect(ads, ref_or_url):
            return a
        raise AdapterError(f"no enabled platform adapter handles '{ref_or_url[:80]}'; enabled: {list(ads)}")

    def _targets(self, boards: list[str] | None, platforms: list[str] | None) -> tuple[list[tuple[Any, str]], list[dict]]:
        ads, cfg = self.adapters, self.settings.platforms
        errors = [{"platform": n, "err": "not enabled/known"} for n in (platforms or []) if n not in ads]
        names = [n for n in (platforms or [n for n in ads if n != "jobspy"]) if n in ads]
        out: list[tuple[Any, str]] = []
        for b in boards or []:
            pn, _, bn = b.partition(":")
            if bn and pn in ads:
                out.append((ads[pn], bn))
                continue
            cands = names if platforms else ([n for n in names if b in cfg.boards.get(n, [])]
                                             or [n for n in names if n not in ("workday", "jobspy")])
            out += [(ads[n], b) for n in cands]
        if not boards:
            for n in names:
                out += [(ads[n], b) for b in cfg.boards.get(n, [])]
            if "jobspy" in names and platforms:
                out.append((ads["jobspy"], ",".join(cfg.jobspy_sites)))
        return list(dict.fromkeys((a, b) for a, b in out)), errors

    def _policy_drop(self, p: Posting) -> bool:
        pol, f = self.settings.policy, p.flags
        if pol.requires_sponsorship and ({"no_sponsorship", "opt_excluded"} & f.keys()):
            return True
        if "citizenship" in f or "phd_only" in f:
            return True
        if pol.max_years_required is not None and (y := years_required(" ".join(f.get("years", [])))) is not None \
                and y > pol.max_years_required:
            return True
        return bool(pol.graduation and "grad_window" in f and grad_window_conflict(f["grad_window"], pol.graduation))

    async def search(self, query: str = "", boards: list[str] | None = None, platforms: list[str] | None = None,
                     location: str = "", since_days: int = 7, limit: int = 25, cursor: int = 0,
                     apply_policy: bool = True, **_: Any) -> dict[str, Any]:
        """Fan out over (platform, board) pairs in parallel; compact, paginated rows (newest first)."""
        targets, errors = self._targets(boards, platforms)
        if not targets:
            return {"rows": [], "total": 0, "next": None, "errors": errors,
                    "hint": "pass boards=['greenhouse:acme', ...] or set [platforms.boards] in config"}
        since = (datetime.now(UTC).date() - timedelta(days=since_days)).isoformat() if since_days else None

        async def one(a: Any, b: str) -> list[Posting]:
            kw = {"location": location} if a.name == "jobspy" else {}
            return [p async for p in a.list_postings(b, since=since, query=query, **kw)]

        results = await bounded_gather((one(a, b) for a, b in targets), self.settings.concurrency.global_limit)
        terms = [t for t in query.lower().split() if t]
        loc = location.lower().strip()
        rows: list[Posting] = []
        dropped = 0
        for (a, b), res in zip(targets, results):
            if isinstance(res, BaseException):
                errors.append({"board": f"{a.name}:{b}", "err": str(res)[:120]})
                continue
            server_q = getattr(a, "name", "") in ("workday", "jobspy")
            for p in res:
                if terms and not server_q and not all(t in p.title.lower() for t in terms):
                    continue
                if loc and a.name != "jobspy" and loc not in p.location.lower():
                    continue
                if apply_policy and self._policy_drop(p):
                    dropped += 1
                    continue
                rows.append(p)
        rows.sort(key=lambda p: (p.posted_at or "", p.ref), reverse=True)
        page = rows[cursor: cursor + limit]
        nxt = cursor + limit if cursor + limit < len(rows) else None
        out: dict[str, Any] = {"rows": [{**p.compact(), "d": p.posted_at} for p in page], "total": len(rows), "next": nxt}
        if dropped:
            out["filtered"] = dropped
        if errors:
            out["errors"] = errors[:10]
        return out

    async def get_job(self, ref_or_url: str, fields: list[str] | None = None) -> dict[str, Any]:
        """Compact posting; opt in with fields: description, url, apply_url, posted, flags_detail."""
        a = self.resolve(ref_or_url)
        want = set(fields or [])
        p = await a.get_posting(ref_or_url, with_description="description" in want)
        out = {**p.compact(), "platform": p.platform}
        if "url" in want:
            out["url"] = p.url
        if want & {"apply_url", "url"} and p.apply_url:
            out["apply_url"] = p.apply_url
        if "posted" in want or "all" in want:
            out["posted"] = p.posted_at
        if "flags_detail" in want:
            out["flags"] = p.flags
        if p.description:
            out["description"] = p.description
        return out

    async def get_form(self, ref_or_url: str) -> dict[str, Any]:
        a = self.resolve(ref_or_url)
        f = await a.get_form(ref_or_url)
        rows = []
        for x in f.fields:
            r: dict[str, Any] = {"k": x.key, "l": x.label[:80], "t": x.kind.value}
            if x.required:
                r["r"] = 1
            if x.options:
                r["o"] = x.options[:12]
            rows.append(r)
        return {"ref": f.ref, "via": f.submit_via, "fields": rows}

    async def plan(self, ref_or_url: str) -> dict[str, Any]:
        raise NotImplementedError("apply engine lands in feat/apply-engine")

    async def submit(self, ref_or_url: str, answers: dict[str, Any], dry_run: bool | None) -> dict[str, Any]:
        raise NotImplementedError("apply engine lands in feat/apply-engine")

    async def apply_batch(self, refs: list[str], dry_run: bool | None, max_parallel: int | None) -> dict[str, Any]:
        limit = max_parallel or self.settings.concurrency.browser_workers
        rows = await bounded_gather((self.submit(r, {}, dry_run) for r in refs), limit)
        return {"results": [r if not isinstance(r, BaseException) else {"error": str(r)} for r in rows]}

    async def list_applications(self, status: str, limit: int, cursor: int) -> dict[str, Any]:
        page = await self.store.list(status="" if status in ("", "all") else status, limit=limit, cursor=cursor)
        rows = [{"ref": r["ref"], "co": r["company"], "title": r["title"], "st": r["status"],
                 "at": r["created_at"][:10]} for r in page["rows"]]
        return {"rows": rows, "next": page["next"], "total": page["total"]}

    async def health(self) -> dict[str, Any]:
        from .core import registry

        return {"platforms": registry.platforms.names(), "llm": registry.llm_providers.names(),
                "browser": registry.browser_drivers.names(), "dry_run": self.settings.dry_run}

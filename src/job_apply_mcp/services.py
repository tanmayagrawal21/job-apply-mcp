"""Service layer: wires registries + config into the operations the tools expose.

Component PRs fill in the TODOs; each method must stay usable (raising NotImplementedError with a clear
message) until its component lands, so the server always starts.
"""
from __future__ import annotations

import asyncio
import contextlib
import json
import time
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from .config import Settings
from .core.concurrency import HostLimiter, bounded_gather
from .core.interfaces import PlatformAdapter
from .core.models import Posting
from .platforms import AdapterError, HttpClient, build_adapters, detect
from .platforms.textflags import grad_window_conflict, years_required
from .search.sessions import Session, SessionError, SessionStore, parse_cursor


class Services:
    def __init__(self, settings: Settings, engine: Any = None):
        self.settings = settings
        self._engine = engine
        c = settings.concurrency
        self._prov = None
        self.hosts = HostLimiter(per_host=c.per_host, min_interval_s=c.min_interval_s,
                                 overrides={h: (v.per_host, v.min_interval_s) for h, v in c.hosts.items()})
        self._sessions: SessionStore | None = None
        self._http: HttpClient | None = None
        self._adapters: dict[str, PlatformAdapter] | None = None
        self._store = None
        self._llm = None
        self._skills: list[str] | None = None
        self._desc: dict[str, Posting | None] = {}
        self._late: set[asyncio.Task] = set()   # timed-out board fetches left to finish into the cache (ADR-0043)

    @property
    def improve(self):
        from .improve.service import ImproveService

        if getattr(self, "_improve", None) is None:
            self._improve = ImproveService(self.settings)
        return self._improve

    async def request_feature(self, description: str, kind: str | None = None) -> dict[str, Any]:
        return await self.improve.request_feature(description, kind)

    async def improvement_report(self, limit: int = 10) -> dict[str, Any]:
        return await self.improve.improvement_report(limit)

    @property
    def llm(self):
        """Task-class + complexity router (ADR-0008), created lazily."""
        if self._llm is None:
            from .llm import LLMRouter

            self._llm = LLMRouter(self.settings)
        return self._llm

    @property
    def store(self):
        """SQLite store, opened lazily so the server starts even if the data dir is unwritable."""
        if self._store is None:
            from .store.sqlite import SqliteStore

            self._store = SqliteStore(self.settings.paths.data_dir, dedupe_days=self.settings.policy.dedupe_days,
                                 dedupe_mode=self.settings.dedupe.mode,
                                      company_aliases=self.settings.dedupe.company_aliases,
                                      title_threshold=self.settings.dedupe.title_threshold)
        return self._store

    @property
    def engine(self):
        """Apply engine, built lazily (ADR-0011); raises ComponentMissing with a clear message if a
        required sibling component is not installed."""
        if self._engine is None:
            from .apply.adapters import RouterView
            from .apply.factory import build_engine

            self._engine = build_engine(self.settings, store=self.store, resolve_adapter=self.resolve,
                                        router=RouterView(self.llm))
        return self._engine

    # ------------------------------------------------------------------ platform plumbing
    @property
    def http(self) -> HttpClient:
        if self._http is None:
            p = self.settings.platforms
            self._http = HttpClient(
                cache_dir=Path(self.settings.paths.data_dir).expanduser() / "http_cache", ttl_s=p.cache_ttl_s,
                limiter=self.hosts, timeout_s=p.timeout_s, retries=p.retries, backoff_s=p.backoff_s,
                user_agent=p.user_agent, prune_days=p.cache_prune_days, memo_entries=p.memo_boards)
        return self._http

    @property
    def adapters(self) -> dict[str, PlatformAdapter]:
        if self._adapters is None:
            self._adapters = build_adapters(self.settings.platforms_enabled, self.http, self.settings.platforms)
        return self._adapters

    async def aclose(self) -> None:
        for t in list(self._late):
            t.cancel()
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
        return self._interleave(list(dict.fromkeys((a, b) for a, b in out))), errors

    @staticmethod
    def _interleave(targets: list[tuple[Any, str]]) -> list[tuple[Any, str]]:
        """Round-robin the targets across platforms (ADR-0043). In config order every global slot first fills with
        one platform's boards, so that host's per-host cap idles the rest and the hosts run one after another."""
        by: dict[str, list[tuple[Any, str]]] = {}
        for t in targets:
            by.setdefault(getattr(t[0], "name", ""), []).append(t)
        queues = list(by.values())
        return [q[i] for i in range(max(map(len, queues), default=0)) for q in queues if i < len(q)]

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

    @property
    def sessions(self) -> SessionStore:
        if self._sessions is None:
            c = self.settings.search.sessions
            d = Path(self.settings.paths.data_dir).expanduser() / "search_sessions" if c.persist else None
            self._sessions = SessionStore(ttl_s=c.ttl_s, max_sessions=c.max_sessions, directory=d)
            self._sessions.prune()
        return self._sessions

    def search_status(self, session_id: str) -> dict[str, Any]:
        """Progress/state of a search session (running, done, or expired)."""
        try:
            s = self.sessions.get(session_id.split(":", 1)[0])
        except SessionError as e:
            return {"error": str(e)}
        return {**s.progress(), "total": s.meta.get("total", len(s.rows))}

    async def search(self, query: str = "", boards: list[str] | None = None, platforms: list[str] | None = None,
                     location: str = "", since_days: int = 7, limit: int = 25, cursor: int | str | None = 0,
                     apply_policy: bool = True, progress: Any = None, **_: Any) -> dict[str, Any]:
        """Fan out over (platform, board) pairs once into a session, then page from it (newest/best first).

        `cursor` is the opaque `next` of a previous page (`<session_id>:<offset>`); pages never touch the network.
        """
        try:
            sid, offset = parse_cursor(cursor)
            sess = self.sessions.get(sid) if sid else None
        except SessionError as e:
            return {"rows": [], "total": 0, "next": None, "error": str(e)}
        if sess is not None and sess.status == "running":
            return {"rows": [], "total": 0, "next": f"{sess.sid}:{offset}", "progress": sess.progress(),
                    "hint": "search still running; poll search_status then retry this cursor"}
        if sess is None:
            sess = self.sessions.create()
            try:
                await self._build_session(sess, query, boards, platforms, location, since_days, apply_policy, progress)
            except BaseException:
                sess.status = "error"
                raise
            finally:
                sess.elapsed_s = time.monotonic() - sess.started
                if sess.status == "running":
                    sess.status = "done"
                self.sessions.save(sess)
        return self._page(sess, offset, limit)

    def _page(self, sess: Session, cursor: int, limit: int) -> dict[str, Any]:
        rows, meta = sess.rows, sess.meta
        limit = max(1, limit)
        end = min(cursor + limit, len(rows))
        page = rows[cursor:end]

        def nxt(e: int) -> str | None:
            if e >= len(rows):
                return None
            return f"{sess.sid}:{e}" if self.settings.search.sessions.enabled else e   # type: ignore[return-value]

        out: dict[str, Any] = {"rows": page, "total": len(rows), "next": nxt(end), "session": sess.sid, **meta.get("extra", {})}
        if meta.get("filtered"):
            out["filtered"] = meta["filtered"]
        if meta.get("errors"):
            out["errors"] = meta["errors"][:10]
        if meta.get("timings"):
            out["timings"] = meta["timings"]
        budget = self.settings.response_budget_chars   # shrink the page rather than let the server truncate JSON
        while len(page) > 1 and len(json.dumps(out, separators=(",", ":"), default=str)) > budget:
            page.pop()
            out["next"] = nxt(cursor + len(page))
        out["rows"] = list(page)
        return out

    async def _build_session(self, sess: Session, query: str, boards: list[str] | None, platforms: list[str] | None,
                             location: str, since_days: int, apply_policy: bool, progress: Any) -> None:
        targets, errors = self._targets(boards, platforms)
        sess.boards_total = len(targets)
        if not targets:
            sess.meta = {"errors": errors,
                         "extra": {"hint": "pass boards=['greenhouse:acme', ...] or set [platforms.boards] in config"}}
            return
        since = (datetime.now(UTC).date() - timedelta(days=since_days)).isoformat() if since_days else None
        step = max(1, len(targets) // 50)
        cfg = self.settings.search
        t_start, http = time.monotonic(), self.http
        t0, s0 = dict(http.timings), dict(http.stats)
        slow: list[tuple[float, str]] = []

        async def collect(a: Any, b: str, kw: dict[str, Any]) -> list[Posting]:
            return [p async for p in a.list_postings(b, since=since, query=query, **kw)]

        async def one(a: Any, b: str) -> list[Posting]:
            kw = {"location": location} if a.name == "jobspy" else {}
            t = time.monotonic()
            task = asyncio.ensure_future(collect(a, b, kw))
            try:   # per-board budget so one slow board cannot stall the fan-out (ADR-0043)
                return await asyncio.wait_for(asyncio.shield(task), cfg.board_timeout_s or None)
            except TimeoutError:
                # the fetch keeps running so its response lands in the HTTP cache for the next search
                self._late.add(task)
                task.add_done_callback(lambda tk: (self._late.discard(tk), tk.cancelled() or tk.exception()))
                sess.boards_failed += 1
                raise TimeoutError(f"timed out after {cfg.board_timeout_s:g}s") from None
            except BaseException:
                sess.boards_failed += 1
                raise
            finally:
                slow.append((time.monotonic() - t, f"{a.name}:{b}"))
                sess.boards_done += 1
                if progress and (sess.boards_done % step == 0 or sess.boards_done == sess.boards_total):
                    with contextlib.suppress(Exception):   # a broken client channel must not fail the search
                        await progress(sess.boards_done, sess.boards_total)

        results = await bounded_gather((one(a, b) for a, b in targets), self.settings.concurrency.global_limit)
        t_fetch = time.monotonic()
        terms = [t for t in query.lower().split() if t]
        loc = location.lower().strip()
        rows: list[Posting] = []
        server_side: set[str] = set()   # refs whose platform already applied the query server-side
        dropped = 0
        for (a, b), res in zip(targets, results, strict=True):
            if isinstance(res, BaseException):
                errors.append({"board": f"{a.name}:{b}", "err": str(res)[:120]})
                continue
            server_q = getattr(a, "name", "") in ("workday", "jobspy")
            for p in res:
                if terms and not server_q and cfg.match == "title" and not all(t in p.title.lower() for t in terms):
                    continue
                if loc and a.name != "jobspy" and loc not in p.location.lower():
                    continue
                if apply_policy and self._policy_drop(p):
                    dropped += 1
                    continue
                if server_q:
                    server_side.add(p.ref)
                rows.append(p)
        extra: dict[str, Any] = {}
        scored: dict[str, tuple[float, list[str]]] = {}
        t_rank = time.monotonic()
        if cfg.rank:
            rows, scored, extra, d2 = await self._rank(rows, query, server_side, apply_policy)
            dropped += d2
        else:
            rows.sort(key=lambda p: (p.posted_at or "", p.ref), reverse=True)
        t_end = time.monotonic()
        out_rows = []
        for p in rows[: cfg.sessions.max_rows]:
            r = {**p.compact(), "d": p.posted_at}
            if p.ref in scored:
                r["score"], r["why"] = scored[p.ref][0], scored[p.ref][1]
            out_rows.append(r)
        sess.rows = out_rows
        slow.sort(reverse=True)
        timings = {"total_s": t_end - t_start, "fetch_s": t_fetch - t_start, "filter_s": t_rank - t_fetch,
                   "rank_s": t_end - t_rank, **{k: http.timings[k] - t0.get(k, 0.0) for k in http.timings}}
        sess.meta = {"extra": extra, "filtered": dropped, "errors": errors, "timings": {
            **{k: round(v, 2) for k, v in timings.items()},
            "n": {k: http.stats[k] - s0.get(k, 0) for k in ("requests", "cache_hits", "memo_hits", "revalidated")},
            "slowest": [[b, round(d, 1)] for d, b in slow[:3]]}}

    # ------------------------------------------------------------------ relevance ranking (ADR-0022)
    def _profile_skills(self) -> list[str]:
        if self._skills is None:
            sk = list(self.settings.search.skills)
            from .profile import load_profile

            with contextlib.suppress(OSError, ValueError):  # no readable profile: use search.skills only
                sk += load_profile(self.settings.paths.profile).skills
            self._skills = list(dict.fromkeys(x for x in sk if x.strip()))
        return self._skills

    async def _fetch_description(self, p: Posting) -> Posting | None:
        """Description for ranking, cached per ref for the process (HTTP responses are also disk-cached)."""
        if p.ref in self._desc:
            return self._desc[p.ref]
        try:
            full = await self.adapters[p.platform].get_posting(p.ref, with_description=True)
        except (AdapterError, ValueError, KeyError):  # one bad posting must not fail the search; it stays ranked on title only
            full = None
        self._desc[p.ref] = full
        return full

    async def _rank(self, rows: list[Posting], query: str, server_side: set[str], apply_policy: bool,
                    ) -> tuple[list[Posting], dict[str, tuple[float, list[str]]], dict[str, Any], int]:
        from .search.ranking import embed_rerank, rank_postings

        cfg, skills = self.settings.search, self._profile_skills()
        pol = self.settings.policy
        stage1 = rank_postings(rows, query, skills, cfg, pol)
        head, tail = stage1[: cfg.rank_candidates], stage1[cfg.rank_candidates:]
        need = [r.posting for r in head if r.posting.description is None and r.posting.platform in self.adapters]
        fetched = await bounded_gather((self._fetch_description(p) for p in need), cfg.description_concurrency)
        full = {p.ref: f for p, f in zip(need, fetched, strict=True) if isinstance(f, Posting)}
        dropped, cands = 0, []
        for r in head:
            p = full.get(r.posting.ref)
            if p is not None:
                p = p.model_copy(update={"location": p.location or r.posting.location})
                if apply_policy and self._policy_drop(p):   # description may reveal hard constraints
                    dropped += 1
                    continue
                cands.append(p)
            else:
                cands.append(r.posting)
        ranked = rank_postings(cands, query, skills, cfg, pol)
        if cfg.embeddings and ranked:
            ranked = await embed_rerank(ranked, query, ", ".join(skills), cfg)
        keep = [r for r in ranked if (r.q_hit or r.posting.ref in server_side) and r.score >= cfg.min_score]
        unscanned = 0
        for r in tail:   # beyond the description budget: only title-level evidence, so a query miss is dropped
            if r.q_hit or r.posting.ref in server_side or not query.strip():
                keep.append(r)
            else:
                unscanned += 1
        scored = {r.posting.ref: (r.score, r.why) for r in keep}
        extra = {"unscanned": unscanned} if unscanned else {}
        return [r.posting for r in keep], scored, extra, dropped

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
        return (await self.engine.plan(ref_or_url)).compact()

    async def draft(self, ref_or_url: str) -> dict[str, Any]:
        return (await self.engine.draft(ref_or_url)).compact()

    async def submit(self, ref_or_url: str, answers: dict[str, Any], dry_run: bool | None,
                     approval_id: str | None = None, approver: Any = None) -> dict[str, Any]:
        r = await self.engine.submit(ref_or_url, answers, approval_id, dry_run, approver)
        st = self.engine.approvals.state(ref_or_url)
        return self._submit_row(r, st, self.engine.timings.get(ref_or_url))

    def _submit_row(self, r: Any, st: Any = None, t: Any = None) -> dict[str, Any]:
        row = {"ref": r.ref, "status": r.status, "via": r.via, "confirmation": r.confirmation, "detail": r.detail,
               "pending_id": r.pending_id, "state": st["state"] if st else None, "t": t,
               "client_tasks": getattr(r, "client_tasks", None)}
        if st and st["state"] in ("submitting", "unknown", "submitted", "handoff"):   # ADR-0025
            row["reconcile_in_min"] = (self.settings.ingest.reconcile_delays_min or [None])[0]
        return {k: v for k, v in row.items() if v}

    async def provide_verification_code(self, pending_id: str, code: str) -> dict[str, Any]:
        r = await self.engine.provide_verification_code(pending_id, code)
        return self._submit_row(r, self.engine.approvals.state(r.ref) if r.ref else None)

    async def resolve_submission(self, ref: str, outcome: str, note: str, actor: str,
                                  evidence: str = "") -> dict[str, Any]:
        return await self.engine.resolve_submission(ref, outcome, note, actor, evidence)

    async def apply_batch(self, refs: list[str], dry_run: bool | None, max_parallel: int | None) -> dict[str, Any]:
        return {"results": await self.engine.apply_batch(refs, dry_run, max_parallel)}

    async def list_applications(self, status: str, limit: int, cursor: int) -> dict[str, Any]:
        page = await self.store.list(status="" if status in ("", "all") else status, limit=limit, cursor=cursor)
        rows = [{"ref": r["ref"], "co": r["company"], "title": r["title"], "st": r["status"],
                 "at": r["created_at"][:10]} for r in page["rows"]]
        return {"rows": rows, "next": page["next"], "total": page["total"]}

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
                "browser": registry.browser_drivers.names(), "dry_run": self.settings.dry_run,
                "llm_router": await self.llm.health()}

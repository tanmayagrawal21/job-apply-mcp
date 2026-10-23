"""run_applications (ADR-0045): explicit links, emailed links and discovery through one pipeline.

Order of a run:
  1. replay stored feedback onto policy / profile answers; list what was new since the last run
  2. collect candidates in priority order: explicit links > emailed links > discovery
  3. adapter detection (an unknown host becomes an `unsupported_site` row; it never costs a plan slot)
  4. plan every candidate concurrently with prose deferred (writing.mode="deferred" for this task only); with the
     essay autodraft (ADR-0057) the open essays of prose-only postings are then drafted and strictly reviewed locally,
     one posting at a time in priority order, while the run still has room
  5. take the first `max_applications` eligible postings (ready to submit, or needing the owner), the same way in
     dry and live mode (ADR-0049); skipped and ineligible ones never count. Submit the ready ones concurrently
     across hosts, at most 1 per host, under intake.submit_parallel; the engine's gates, dedupe, approvals, control
     layer and dry_run all still apply
  6. write the run report (items + summary; one status per posting, see store.CATEGORIES) and, in a live run only,
     mark fully handled mail messages as processed. A dry run also previews what the live quota would do (`live_preview`).
"""
from __future__ import annotations

import asyncio
import contextlib
import math
from collections import OrderedDict
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

from ..core.concurrency import bounded_gather
from ..core.models import Decision
from .feedback import FeedbackError, apply_overlays, describe, needs_confirmation, normalize
from .links import JobUrlFilter, clean_url, make_link_source, resolve_filter
from .store import CATEGORIES, LEGACY_STATUS, IntakeStore

SOURCES = ("links", "mail", "discovery")
_RUN_LOCKS: dict[str, asyncio.Lock] = {}   # one run at a time per data dir (process-wide)
_SLD = {"co", "com", "ac", "org", "net", "gov", "edu", "or", "ne", "go"}


def _assumed_value(a: Any) -> Any:
    """Run-report view of an assumed answer: autodrafted essays are shortened (full text in the evidence folder)."""
    from ..apply import essays
    return essays.short(a.value) if essays.is_essay(a.source) else a.value


def site_key(host: str) -> str:
    """Registrable domain without a suffix list: last two labels, or three for a ccTLD second level (x.co.uk)."""
    parts = host.lower().rstrip(".").split(".")
    n = 3 if len(parts) >= 3 and len(parts[-1]) == 2 and parts[-2] in _SLD else 2
    return ".".join(parts[-n:])
_SUBMIT_MAP = {"submitted": "submitted", "dry_run": "dry_run_ok", "duplicate": "skipped", "needs_human": "needs_human",
               "handoff": "needs_human", "incomplete": "needs_human", "awaiting_code": "needs_human",
               "needs_review": "needs_human", "roadblock": "unknown", "error": "failed"}


def check_max_applications(v: Any) -> str | None:
    """None (use the default) or a whole number >= 1; anything else is an error, never silently replaced."""
    if v is None:
        return None
    if isinstance(v, bool) or not isinstance(v, int) or v < 1:
        return f"max_applications must be a whole number >= 1 (got {v!r}); omit it to use the default"
    return None


def _quota_hit(detail: str) -> bool:
    return "quota reached" in (detail or "")


@dataclass
class Cand:
    target: str             # URL or platform ref, exactly as passed to the engine
    source: str             # links | mail | discovery
    msg: str | None = None  # mail message key
    status: str = ""
    why: str = ""
    co: str = ""
    title: str = ""
    approval_id: str | None = None
    deferred: bool = False  # not handled this run (over the cap): its mail message stays unprocessed
    assumed: list[dict[str, Any]] | None = None   # ADR-0052: answers the agent assumed for this posting

    def row(self) -> dict[str, Any]:
        r = {"ref": self.target, "url": self.target if self.target.startswith("http") else None,
             "source": self.source, "status": self.status, "why": self.why[:240] or None, "co": self.co or None,
             "title": self.title or None, "approval_id": self.approval_id}
        return r


def _key(target: str) -> str:
    if target.startswith(("http://", "https://")):
        from ..store.sqlite import canonical_url
        return canonical_url(target) or target
    return target


def read_links_file(path: str | Path) -> list[str]:
    out = []
    for line in Path(path).expanduser().read_text(encoding="utf-8").splitlines():
        s = line.strip()
        if s and not s.startswith("#"):
            out.append(s.split()[0])
    return out


class IntakeRunner:
    def __init__(self, settings: Any, *, engine: Any, store: IntakeStore, resolve: Callable[[str], Any],
                 search: Callable[..., Awaitable[dict[str, Any]]] | None = None, self_address: str = "",
                 mail_connect: Any = None, profile_answer_sink: Callable[..., Any] | None = None):
        self.s, self.engine, self.store, self._resolve = settings, engine, store, resolve
        self._search, self._me, self._connect = search, self_address, mail_connect
        self._sink = profile_answer_sink if profile_answer_sink is not None else self._builder_sink()

    def _builder_sink(self) -> Callable[..., Any] | None:
        """The profile builder's answer_profile_question path (ADR-0047). Only rows a human confirmed (or gave on a
        TTY) are ever delivered, and they are delivered as origin="user_written" so the builder stores them as the
        user's answers, not as suggestions. Returns None when the builder is not installed (rows stay pending)."""
        try:
            import inspect

            from ..persona.tools import store_answer
        except ImportError:
            return None
        takes_origin = "origin" in inspect.signature(store_answer).parameters

        def sink(key: str, value: str, scope: str, scope_value: str) -> Any:
            kw = {"origin": "user_written"} if takes_origin else {}   # no origin param: it always stores user_written
            return store_answer(self.s, key, value, scope, scope_value, **kw)
        return sink

    @classmethod
    def from_services(cls, svc: Any) -> IntakeRunner:
        st = getattr(svc, "_intake_store", None)
        if st is None:
            st = svc._intake_store = IntakeStore(svc.settings.paths.data_dir)
        me = ""
        with contextlib.suppress(Exception):   # no readable profile: intake.mail.self_address or the IMAP user
            from ..profile import load_profile
            me = load_profile(svc.settings.paths.profile).contact.email or ""
        return cls(svc.settings, engine=svc.engine, store=st, resolve=svc.resolve, search=svc.search, self_address=me)

    # ------------------------------------------------------------------------------------------ helpers
    def _supported(self, target: str) -> bool:
        try:
            self._resolve(target)
            return True
        except Exception:  # noqa: BLE001 - AdapterError / ComponentMissing: no adapter handles it
            return False

    def _cached(self, target: str) -> Any:
        f = getattr(self.engine, "_cached", None)
        return f(target) if f else None

    def _host(self, c: Cand) -> str:
        """Per-host key = registrable domain, so boards.example-ats.io and job-boards.example-ats.io share one
        slot. A platform ref without a URL keys on its platform."""
        cached = self._cached(c.target)
        url = (cached.posting.apply_url or cached.posting.url) if cached is not None else c.target
        if url.startswith(("http://", "https://")):
            return site_key(urlsplit(url).hostname or url)
        return c.target.split(":")[0]

    def _confirmed(self, r: dict[str, Any]) -> bool:
        return r.get("origin") == "user" or r.get("confirmed_by") is not None

    def _may_apply(self, r: dict[str, Any]) -> bool:
        return self._confirmed(r) or not needs_confirmation(r["kind"], self.s.apply.approval_mode)

    async def _deliver_profile_answers(self, problems: list[str]) -> tuple[list[str], list[dict[str, Any]]]:
        """Hand pending authorization/demographic/attestation answers to the profile builder. Returns
        (delivered descriptions, still pending rows)."""
        pending = await self.store.pending_profile_answers()
        ready = [p for p in pending if p["confirmed"]]   # client-origin answers wait for a human confirmation
        left = [p for p in pending if not p["confirmed"]]
        if not ready or self._sink is None:
            return [], pending
        done = []
        for p in ready:
            try:
                self._sink(p["key"], p["value"], p["scope"], p.get("scope_value", ""))
                done.append(p)
            except Exception as e:  # noqa: BLE001 - keep it pending; the next run retries
                problems.append(f"profile answer {p['key']}: {str(e)[:120]}")
                left.append(p)
        await self.store.mark_delivered([p["id"] for p in done])
        return [describe("profile_answer", p) + " -> answer_profile_question" for p in done], left

    async def _apply_feedback(self, run_id: str) -> tuple[int | None, list[str], list[str], list[str]]:
        rows = await self.store.feedback()
        usable = [r for r in rows if self._may_apply(r)]
        structured = [r for r in usable if r["kind"] not in ("note", "profile_answer")]
        run_size, problems = apply_overlays(self.s, getattr(self.engine, "profile", None), structured)
        pending = [r for r in usable if r["applied_run"] is None]
        applied = [describe(r["kind"], r["payload"]) for r in pending if r["kind"] not in ("note", "profile_answer")]
        notes = [r["payload"]["text"] for r in pending if r["kind"] == "note"]
        await self.store.mark_feedback_applied([r["id"] for r in pending], run_id)
        self._awaiting = [{"id": r["id"], "will_apply": describe(r["kind"], r["payload"]),
                           "confirm": f"job-apply-mcp feedback --confirm {r['id']}"}
                          for r in rows if not self._may_apply(r) and r["kind"] != "profile_answer"]
        return run_size, applied, notes, problems

    async def _mail(self, mail: dict[str, Any] | None, messages: list[dict[str, Any]] | None,
                    notes: list[str]) -> list[tuple[str, list[str]]]:
        mc = self.s.intake.mail
        try:
            src = make_link_source(self.s, messages, connect=self._connect)
        except ValueError as e:
            notes.append(f"mail: {e}")
            return []
        if src is None:
            notes.append("mail: the mail source is 'client'; pass `messages` [{id, from, to, subject, date, body}]")
            return []
        flt, err = resolve_filter(mc, self.s.ingest.imap, self_address=self._me, overrides=mail)
        if err:
            notes.append(f"mail: {err}")
            return []
        try:
            msgs = await asyncio.to_thread(lambda: list(src.fetch(flt, mc.max_messages)))
        except (OSError, ValueError, PermissionError, RuntimeError) as e:
            notes.append(f"mail: {str(e)[:160]}")
            return []
        done = await self.store.processed([m.key for m in msgs])
        is_job = JobUrlFilter(mc, self._supported)
        return [(m.key, [u for u in m.urls if is_job(u)][: mc.max_links_per_message])
                for m in msgs if m.key not in done]

    # ------------------------------------------------------------------------------------------ run
    async def run(self, *, links: list[str] | None = None, links_file: str | None = None,
                  sources: list[str] | None = None, max_applications: int | None = None,
                  dry_run: bool | None = None, mail: dict[str, Any] | None = None,
                  messages: list[dict[str, Any]] | None = None,
                  discovery: dict[str, Any] | None = None) -> dict[str, Any]:
        lock = _RUN_LOCKS.setdefault(str(self.s.paths.data_dir), asyncio.Lock())
        bad_n = check_max_applications(max_applications)
        if bad_n:
            return {"error": bad_n}
        if lock.locked():   # max_applications is per run: two concurrent runs must not each submit n
            return {"error": "another run_applications is in progress; wait for it, then retry"}
        async with lock:
            return await self._run(links=links, links_file=links_file, sources=sources,
                                   max_applications=max_applications, dry_run=dry_run, mail=mail,
                                   messages=messages, discovery=discovery)

    async def _run(self, *, links: list[str] | None, links_file: str | None, sources: list[str] | None,
                   max_applications: int | None, dry_run: bool | None, mail: dict[str, Any] | None,
                   messages: list[dict[str, Any]] | None, discovery: dict[str, Any] | None) -> dict[str, Any]:
        cfg = self.s.intake
        self._awaiting = []
        try:
            dry = self.engine._resolve_dry(dry_run)
        except PermissionError as e:
            return {"error": str(e)}
        srcs = list(sources or cfg.sources)
        bad = [x for x in srcs if x not in SOURCES]
        if bad:
            return {"error": f"unknown sources {bad}; use {list(SOURCES)}"}
        explicit = list(links or [])
        if links_file:
            try:
                explicit += read_links_file(links_file)
            except OSError as e:
                return {"error": f"links file: {e}"}
        run_id = await self.store.start_run({"sources": srcs, "max_applications": max_applications, "dry_run": dry,
                                             "links": len(explicit), "mail": mail or {}, "discovery": discovery or {}})
        cands: list[Cand] = []
        notes: list[str] = []
        applied: list[str] = []
        fb_notes: list[str] = []
        pending_pa: list[dict[str, Any]] = []
        n = 0
        preview: dict[str, Any] | None = None
        try:
            run_size, applied, fb_notes, problems = await self._apply_feedback(run_id)
            delivered, pending_pa = await self._deliver_profile_answers(problems)
            applied += delivered
            notes += problems
            want = max_applications if max_applications is not None else (run_size or cfg.max_applications)
            n = max(1, min(cfg.max_applications_cap, want))
            if want > n:
                notes.append(f"max_applications={want} is above intake.max_applications_cap={n}; using {n}")
            pool_size = math.ceil(n * cfg.plan_factor)
            cands = await self._collect(explicit, srcs, mail, messages, discovery, pool_size, notes)
            await self._plan_and_submit(cands, n, dry)
            preview = self._live_preview(cands) if dry else None
        finally:
            # A dry run applies nothing, so it never consumes a mail message: the live run must still see its links.
            done = {} if dry else self._processed_messages(cands)
            if done:
                await self.store.mark_processed(run_id, done)
            rows = [c.row() for c in cands if c.target]
            await self.store.add_items(run_id, rows)
            ctx = {"applied_feedback": applied, "feedback_notes": fb_notes, "notes": notes,
                   "pending_profile_answers": [
                       {**{k: p[k] for k in ("id", "key", "value", "scope", "scope_value") if p.get(k)},
                        **({} if p["confirmed"] else {"confirm": f"job-apply-mcp feedback --confirm {p['id']}"})}
                       for p in pending_pa],
                   "awaiting_confirmation": getattr(self, "_awaiting", []),
                   # ADR-0052: every assumed answer, labelled, per posting
                   "assumed": {c.target: c.assumed for c in cands if c.target and c.assumed}}
            rep = self._build(run_id, rows, cfg.report_rows)
            await self.store.finish_run(run_id, {**ctx, "counts": rep["counts"], "mode": "dry_run" if dry else "live",
                                          "autonomy": self.s.answers.autonomy})
        out = {**rep, "mode": "dry_run" if dry else "live", "autonomy": self.s.answers.autonomy,
               "max_applications": n or None, "assumed": ctx["assumed"] or None,
               "plan_context": {k: v for k, v in ctx.items() if v and k != "assumed"}}
        if preview is not None:
            out["live_preview"] = preview
        return {k: v for k, v in out.items() if v is not None}

    async def _collect(self, explicit: list[str], srcs: list[str], mail: dict[str, Any] | None,
                       messages: list[dict[str, Any]] | None, discovery: dict[str, Any] | None, pool_size: int,
                       notes: list[str]) -> list[Cand]:
        out: list[Cand] = []
        seen: set[str] = set()
        ready = 0

        def add(target: str, source: str, msg: str | None = None, co: str = "", title: str = "") -> None:
            nonlocal ready
            k = _key(target)
            if k in seen:
                return
            seen.add(k)
            c = Cand(target, source, msg, co=co, title=title)
            if not self._supported(target):
                c.status = "unsupported_site"
                host = urlsplit(target).hostname or target[:60]
                c.why = f"no enabled platform adapter handles {host}"
            elif ready >= pool_size:
                c.status, c.deferred = "skipped", True
                c.why = "max_applications reached before planning" + ("; kept for the next run" if msg else "")
            else:
                ready += 1
            out.append(c)

        for raw in explicit:          # 1. explicit links (always used when given)
            u = clean_url(raw) if raw.lower().startswith(("http://", "https://")) else raw.strip()
            if u:
                add(u, "links")
        if "mail" in srcs and self.s.intake.mail.enabled:   # 2. emailed links
            for key, urls in await self._mail(mail, messages, notes):
                if not urls:
                    out.append(Cand("", "mail", key, status="_empty"))   # processed, never reported
                for u in urls:
                    add(u, "mail", key)
        if "discovery" in srcs and self._search is not None and ready < pool_size:   # 3. discovery
            d = {**self.s.intake.discovery.model_dump(), **{k: v for k, v in (discovery or {}).items() if v is not None}}
            try:
                res = await self._search(query=d["query"], boards=d["boards"] or None,
                                         platforms=d["platforms"] or None, location=d["location"],
                                         since_days=d["since_days"], limit=pool_size - ready, apply_policy=True)
            except Exception as e:  # noqa: BLE001 - a failed search must not fail the run
                notes.append(f"discovery: {type(e).__name__}: {str(e)[:120]}")
                res = {}
            if res.get("hint"):
                notes.append(f"discovery: {res['hint']}")
            for r in res.get("rows", []):
                if ready >= pool_size:
                    break
                add(r["ref"], "discovery", co=r.get("co", ""), title=r.get("title", ""))
        return out

    async def _plan_and_submit(self, cands: list[Cand], n: int, dry: bool) -> None:
        from ..apply.engine import WRITE_MODE

        pool = [c for c in cands if not c.status]
        tok = WRITE_MODE.set(self.s.intake.writing_mode)
        try:   # all at once; the engine's planner semaphore and the host limiter do the bounding
            outs = await bounded_gather((self.engine.plan(c.target) for c in pool), max(1, len(pool)))
        finally:
            WRITE_MODE.reset(tok)
        eligible: list[Cand] = []   # in priority order; status "" = ready to submit
        postings: set[str] = set()
        prose_only: list[tuple[Cand, Any]] = []
        for c, out in zip(pool, outs, strict=True):
            if self._classify(c, out, postings, eligible) == "prose":
                prose_only.append((c, out))
        if prose_only and getattr(self.s, "essay_autodraft", False):
            await self._autodraft(prose_only, pool, postings, eligible, n)
        seen = {id(e) for e in eligible}
        for c, out in prose_only:
            if not c.status and id(c) not in seen:
                c.status = "skipped"
                c.why = f"required prose not approved yet: {self._prose_keys(out)[:4]} (draft_answers, then submit)"
        # The cap counts every eligible posting (ready, or needing the owner) in priority order, in dry and live mode
        # alike (ADR-0049). Skipped, failed and unsupported postings never take a slot.
        order = {id(c): i for i, c in enumerate(pool)}
        eligible.sort(key=lambda c: order[id(c)])
        for c in eligible[n:]:
            c.status, c.why, c.deferred = "skipped", f"max_applications={n} reached", True
        await self._submit_all([c for c in eligible[:n] if not c.status], dry)

    @staticmethod
    def _prose_keys(out: Any) -> list[str]:
        return [t["key"] for t in out.client_tasks if t.get("deferred") or t.get("draft") is not None
                or t.get("timed_out")]

    def _classify(self, c: Cand, out: Any, postings: set[str], eligible: list[Cand]) -> str:
        """Set the candidate's status from its plan. Returns "prose" when only deferred prose is open (the caller
        drafts it, or skips the posting), else ""."""
        if isinstance(out, BaseException):
            c.status, c.why = "failed", f"plan: {type(out).__name__}: {out}"
            return ""
        cached = self._cached(c.target)
        if cached is not None:
            c.co, c.title = cached.posting.company or c.co, cached.posting.title or c.title
        g = out.plan.gate
        c.approval_id = out.approval_id
        c.assumed = [{"key": a.key, "value": _assumed_value(a), "rule": a.source,
                      **({"why": a.rationale} if a.rationale else {})}
                     for a in out.plan.answers if a.assumed] or None
        if g.decision != Decision.submit:
            c.status = "skipped" if g.decision == Decision.skip else "needs_human"
            c.why = "; ".join(g.reasons) or g.decision.value
            if c.status == "needs_human":
                eligible.append(c)
            return ""
        if out.plan.unresolved:
            human = [t["key"] for t in out.client_tasks if t.get("needs_human")]
            prose = self._prose_keys(out)
            if prose and not human and set(out.plan.unresolved) <= set(prose):
                return "prose"
            c.status, c.why = "needs_human", self._unresolved_why(out.plan.unresolved, cached)
            eligible.append(c)
            return ""
        pid = cached.posting.ref if cached is not None else _key(c.target)
        if pid in postings:
            c.status, c.why = "skipped", "duplicate: same posting earlier in this run"
            return ""
        postings.add(pid)
        eligible.append(c)
        return ""

    async def _autodraft(self, prose_only: list[tuple[Cand, Any]], pool: list[Cand], postings: set[str],
                         eligible: list[Cand], n: int) -> None:
        """ADR-0057: draft the open essays (local writer + local strict reviewer, engine.draft) of prose-only postings
        in priority order, one at a time so each gets its full writing.timeout_s, and only while the run still has
        room under max_applications. A rejected or timed-out essay leaves its posting needing the owner."""
        rank = {id(c): i for i, c in enumerate(pool)}
        for c, _ in prose_only:
            ahead = sum(1 for e in eligible if rank[id(e)] < rank[id(c)])
            if ahead >= n:          # a posting this far down the order could never take a slot
                break
            try:
                out = await self.engine.draft(c.target)
            except Exception as e:  # noqa: BLE001 - one posting's drafting must not fail the run
                c.status, c.why = "failed", f"draft: {type(e).__name__}: {e}"
                continue
            if self._classify(c, out, postings, eligible) == "prose":
                c.status, c.why = "needs_human", "essay autodraft left prose open"
                eligible.append(c)

    async def _submit_all(self, todo: list[Cand], dry: bool) -> None:
        by_host: OrderedDict[str, list[Cand]] = OrderedDict()
        for c in todo:
            by_host.setdefault(self._host(c), []).append(c)
        sem = asyncio.Semaphore(self.s.intake.submit_parallel)
        manual = not dry and not self.s.apply.auto_approve_active

        async def one(c: Cand) -> None:
            if manual:   # same rule as apply_batch: a live submit needs its approval first (ADR-0021)
                c.status = "needs_human"
                c.why = f"awaiting_approval: run `job-apply-mcp approve {c.approval_id}`, then submit"
                return
            try:
                r = await self.engine.submit(c.target, {}, c.approval_id, dry)
            except Exception as e:  # noqa: BLE001
                c.status, c.why = "failed", f"{type(e).__name__}: {e}"
                return
            c.status = _SUBMIT_MAP.get(r.status, "unknown")
            if c.status == "needs_human" and _quota_hit(r.detail or ""):
                c.status = "quota_blocked"   # the engine's control layer refused it; nothing was sent
                c.deferred = True            # not final: its mail message stays unprocessed so a later run retries
            c.why = f"{r.status}: {r.detail}" if r.detail and c.status != "submitted" else (r.detail or "")
            st = None
            with contextlib.suppress(Exception):
                st = self.engine.approvals.state(c.target)
            if st and st.get("state") == "unknown":
                c.status = "unknown"

        async def host_worker(items: list[Cand]) -> None:   # one host's jobs run one after another
            for c in items:
                async with sem:
                    await one(c)

        await asyncio.gather(*(host_worker(v) for v in by_host.values()))

    @staticmethod
    def _unresolved_why(keys: list[str], cached: Any) -> str:
        """'14 unresolved (7 shown): <label>, ...'. Labels only (never answer values), redacted, ~60 chars each (#78)."""
        from ..core.secrets import redact

        form = getattr(cached, "form", None)
        labels = {f.key: f.label for f in getattr(form, "fields", [])}
        shown = []
        for k in keys[:7]:
            lab = redact(" ".join(str(labels.get(k) or k).split()))   # redact the whole label, then cut
            shown.append(lab if len(lab) <= 60 else lab[:59] + "…")
        more = ", …" if len(keys) > 7 else ""
        return f"{len(keys)} unresolved ({len(shown)} shown): {', '.join(shown)}{more}"

    def _live_preview(self, cands: list[Cand]) -> dict[str, Any] | None:
        """Dry run only: what a live run of the dry_run_ok rows would do under the current quotas and approval mode.
        Read-only: it reads the control layer's usage (trusted config) and never changes a limit (ADR-0049)."""
        ok = [c for c in cands if c.status == "dry_run_ok"]
        out: dict[str, Any] = {"would_submit": 0, "quota_blocked": 0}   # always present on a dry run (#78)
        notes: list[str] = []
        if self.s.dry_run:
            notes.append("global dry_run is on: a live run is refused until dry_run=false in the user config")
        if not self.s.apply.auto_approve_active:
            notes.append("approval_mode needs a human: a live run hands each one off for approval "
                         "(needs_human, awaiting_approval)")
        st: dict[str, Any] = {}
        status = getattr(getattr(self.engine, "control", None), "daily_limit_status", None)
        if callable(status):
            try:
                st = status() or {}
            except Exception as e:  # noqa: BLE001 - a preview must never fail the run
                notes.append(f"quota usage unreadable ({type(e).__name__}); the live run still enforces it")
        if st.get("error"):
            notes.append(f"quota config error: {st['error'][:160]} (a live run blocks every submit)")
        lim, used = st.get("limit") or {}, st.get("used") or {}
        if lim:
            day_left = None if lim.get("day") is None else max(0, lim["day"] - used.get("day", 0))
            run_left = None if lim.get("run") is None else max(0, lim["run"] - used.get("run", 0))
            plat_used = dict(used.get("platform") or {})
            would, blocked = 0, []
            for c in ok:
                cached = self._cached(c.target)
                plat = (cached.posting.platform if cached is not None else "") or c.target.split(":")[0]
                pu = plat_used.get(plat, 0)
                by = ("day" if day_left is not None and day_left <= 0 else
                      "run" if run_left is not None and run_left <= 0 else
                      f"platform:{plat}" if lim.get("platform") is not None and pu >= lim["platform"] else "")
                if by:
                    blocked.append({"ref": c.target, "by": by})
                    continue
                would += 1
                plat_used[plat] = pu + 1
                day_left = None if day_left is None else day_left - 1
                run_left = None if run_left is None else run_left - 1
            out["would_submit"] = would
            plats = sorted({*(used.get("platform") or {}), *plat_used})
            out["remaining"] = {"day": None if lim.get("day") is None else max(0, lim["day"] - used.get("day", 0)),
                                "platform": {p: None if lim.get("platform") is None
                                             else max(0, lim["platform"] - (used.get("platform") or {}).get(p, 0))
                                             for p in plats}}
            if blocked:
                out["quota_blocked"] = len(blocked)
                out["blocked"] = blocked[: self.s.intake.report_rows]
                caps = sorted({b["by"] for b in blocked})
                used_s = []
                for b in caps:
                    if b.startswith("platform:"):
                        p = b.split(":", 1)[1]
                        used_s.append(f"{p} {used.get('platform', {}).get(p, 0)}/{lim['platform']}")
                    else:
                        used_s.append(f"{b} {used.get(b, 0)}/{lim[b]}")
                notes.append(f"live quota would block {len(blocked)} of {len(ok)} ({', '.join(used_s)}); a live "
                             f"run would submit {would} and report the rest as quota_blocked")
        if notes:
            out["warning"] = notes
        return out

    @staticmethod
    def _processed_messages(cands: list[Cand]) -> dict[str, int]:
        per: dict[str, list[Cand]] = {}
        for c in cands:
            if c.msg:
                per.setdefault(c.msg, []).append(c)
        return {k: sum(1 for c in v if c.target) for k, v in per.items()
                if all(c.status and not c.deferred for c in v)}

    # ------------------------------------------------------------------------------------------ report
    @staticmethod
    def _build(run_id: str, rows: list[dict[str, Any]], limit: int, category: str = "") -> dict[str, Any]:
        rows = [{**r, "status": LEGACY_STATUS.get(r["status"], r["status"])} for r in rows]
        rows = [r for r in rows if r["status"] in CATEGORIES]
        cats: dict[str, list[dict[str, Any]]] = {k: [] for k in CATEGORIES}
        for r in rows:
            cats[r["status"]].append({k: v for k, v in (("ref", r["ref"]), ("src", r["source"]), ("co", r.get("co")),
                                                        ("title", r.get("title")), ("why", r.get("why")),
                                                        ("approval_id", r.get("approval_id")
                                                         if r["status"] == "needs_human" else None)) if v})
        out: dict[str, Any] = {"run_id": run_id, "counts": {k: len(v) for k, v in cats.items() if v}}
        for k, v in cats.items():
            if not v or (category and k != category):
                continue
            out[k] = v[:limit]
            if len(v) > limit:
                out[f"{k}_more"] = len(v) - limit
        return out

    async def report(self, run_id: str = "", category: str = "", limit: int | None = None) -> dict[str, Any]:
        run = await self.store.get_run(run_id)
        if run is None:
            return {"error": f"unknown run {run_id!r}" if run_id else "no runs yet"}
        items = await self.store.items(run["run_id"])
        category = LEGACY_STATUS.get(category, category)
        if category and category not in CATEGORIES:
            return {"error": f"unknown category {category!r}; use one of {list(CATEGORIES)}"}
        book = None
        with contextlib.suppress(Exception):
            book = self.engine.approvals
        for it in items:   # reconciliation may have confirmed a submission since the run ended (ADR-0025)
            if book is not None and LEGACY_STATUS.get(it["status"], it["status"]) in ("submitted", "unknown",
                                                                                         "needs_human"):
                st = book.state(it["ref"])
                if st and st.get("state") == "confirmed":
                    it["status"] = "confirmed"
                    await self.store.set_item_status(it["id"], "confirmed")
        rep = self._build(run["run_id"], items, limit or self.s.intake.report_rows, category)
        summ = run["summary"]
        ctx = {k: summ.get(k) for k in ("applied_feedback", "feedback_notes", "notes", "pending_profile_answers",
                                 "awaiting_confirmation")
               if summ.get(k)}
        return {**rep, "started": run["started_at"], "finished": run["finished_at"], "mode": summ.get("mode"),
                "autonomy": summ.get("autonomy") or "strict", **({"assumed": summ["assumed"]} if summ.get("assumed") else {}),
                **({"plan_context": ctx} if ctx else {})}

    async def confirm_feedback(self, fid: int, by: str) -> dict[str, Any]:
        r = await self.store.confirm_feedback(fid, by)
        if r is None:
            return {"error": f"unknown feedback id {fid}"}
        return {"id": fid, "kind": r["kind"], "confirmed_by": r["confirmed_by"] or "user (CLI)",
                "will_apply": describe(r["kind"], r["payload"]), "when": "at the start of the next run"}

    async def give_feedback(self, run_id: str, ref: str | None, feedback: str | dict[str, Any],
                            origin: str = "client") -> dict[str, Any]:
        """origin: "client" (MCP tool) or "user" (the CLI on a TTY). Client-origin answers, and in human approval
        mode salary changes, wait for a human confirmation before any run applies them."""
        run = await self.store.get_run(run_id) if run_id else None
        if run_id and run is None:
            return {"error": f"unknown run {run_id!r}"}
        item = await self.store.find_item(run_id, ref) if (run_id and ref) else None
        if ref and run_id and item is None:
            return {"error": f"ref {ref[:80]!r} is not part of run {run_id}"}
        sizes = [r["payload"]["value"] for r in await self.store.feedback() if r["kind"] == "run_size"]
        cur = sizes[-1] if sizes else self.s.intake.max_applications
        try:
            kind, payload = normalize(feedback, item={"company": item["company"], "title": item["title"]} if item else None,
                                      run_size=cur, cap=self.s.intake.max_applications_cap,
                                      stop_phrases=self.s.policy.stop_phrases)
        except FeedbackError as e:
            return {"error": str(e)}
        fid = await self.store.add_feedback(run_id or None, ref, kind, payload, origin=origin)
        confirm = origin == "client" and needs_confirmation(kind, self.s.apply.approval_mode)
        when = "at the start of the next run"
        if kind == "profile_answer":
            when += (", via the profile builder's answer_profile_question (kept pending and listed in run reports "
                     "until it is installed)")
        out = {"id": fid, "kind": kind, "will_apply": describe(kind, payload), "when": when}
        if confirm:
            out |= {"needs_confirmation": True, "confirm": f"job-apply-mcp feedback --confirm {fid}",
                    "why": "a client-supplied answer (or, in human approval mode, a pay change) applies only after "
                           "the user confirms it"}
        return out

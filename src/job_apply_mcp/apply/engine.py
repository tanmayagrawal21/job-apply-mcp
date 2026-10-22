"""Apply engine (ADR-0011, ADR-0021): plan -> server-held approval -> submit (state machine), plus a batch.

Collaborators are injected and typed by the Protocols in ports.py / core/interfaces.py, so the engine is
testable with small fakes and independent of how profile, router, gates and store are implemented.
"""
from __future__ import annotations

import asyncio
import contextlib
import json
import re
import time
from collections.abc import Awaitable, Callable
from contextlib import contextmanager
from dataclasses import dataclass, field
from typing import Any

from pydantic import BaseModel, Field

from ..config import Settings
from ..control.builtin import _NOT_SENT
from ..core.concurrency import bounded_gather
from ..core.interfaces import BrowserDriver, PlatformAdapter, Store
from ..core.models import (
    Answer,
    ApplicationForm,
    ApplicationPlan,
    Decision,
    FieldKind,
    FormField,
    GateResult,
    Posting,
    SubmitResult,
)
from .approvals import ApprovalBook, evidence_path
from .mapper import (
    DEMOGRAPHIC_KEYS,
    SEMANTIC_SENSITIVE,
    best_option,
    canonical_key,
    geo_kind,
    looks_fabricated,
    map_fields,
    normalize,
    polarity,
    tokens,
)
from .ports import ComponentMissing, ControlLike, GatesLike, ProfileLike, RouterLike
from .salary import compute_ask, format_ask, is_salary_field
from .writing import load_exemplar, summarize_posting, write_text

# Fields the local LLM must never guess: a wrong answer is a false legal/biographical statement.
_SENSITIVE_KEYS = SEMANTIC_SENSITIVE | {"work_authorized", "requires_sponsorship", "gender", "hispanic", "race", "veteran",
                   "disability", "over_18", "privacy_ack", "salary_expectation", "start_date", "graduation",
                   "pronouns", "sexual_orientation"}
_SENSITIVE_LABEL = re.compile(r"how many years|years of|criminal|convict|felony|clearance|citizen|"
                              r"agree|certify|attest|signature|ssn|social security|graduat|\bdate\b|semester|\bterm\b|"
                              r"quarter|pronoun|gender|ethnic|\brace\b|veteran|disabilit", re.IGNORECASE)
# Attestations and AI-use questions: answered only from an explicit profile [answers] pattern, else a human.
_HUMAN_LABEL = re.compile(
    r"\bai\b|artificial intelligence|\bllms?\b|chatgpt|copilot|generative|certif|attest|foreign talent|"
    r"own work|by selecting|i agree|i acknowledge|i declare|under penalty|true and (?:correct|accurate)|"
    r"truthful|export control|conflict of interest", re.IGNORECASE)
# Never fabricated: contact/link values exist only in the profile.
_NO_FABRICATE_LABEL = re.compile(r"\burl\b|link|website|web site|e-?mail|phone|mobile|telephone|username|"
                                 r"user name|handle|linkedin|github|twitter|portfolio|\bprofile\b|address",
                                 re.IGNORECASE)
_EXPERIENCE_LABEL = re.compile(r"experience|proficien|familiar|skilled|knowledge of|expertise|comfortable with|"
                               r"do you (?:know|use|have)|years", re.IGNORECASE)
_GENERIC = {"experience", "years", "year", "have", "you", "with", "any", "work", "working", "professional",
            "using", "use", "that", "this", "about", "what", "how", "many", "much", "the", "and", "your",
            "prior", "previous", "relevant", "hands", "knowledge", "familiar", "proficient", "proficiency",
            "skills", "skill", "level", "are", "does", "than", "more", "least", "following", "please"}


class PlanOut(BaseModel):
    plan: ApplicationPlan
    client_tasks: list[dict[str, Any]] = Field(default_factory=list)
    approval_id: str | None = None
    timings: dict[str, int] = Field(default_factory=dict)

    def compact(self) -> dict[str, Any]:
        d: dict[str, Any] = {"ref": self.plan.ref, "gate": self.plan.gate.decision.value}
        if self.plan.gate.reasons:
            d["why"] = self.plan.gate.reasons
        if self.plan.answers:
            d["answers"] = {a.key: a.value for a in self.plan.answers}
            if self.plan.audit:
                d["audit"] = self.plan.audit
            d["src"] = {a.key: a.source for a in self.plan.answers if a.source != "profile"}
        if self.plan.unresolved:
            d["unresolved"] = self.plan.unresolved
        if self.client_tasks:
            d["client_tasks"] = self.client_tasks
        if self.approval_id:
            d["approval_id"] = self.approval_id
        if self.timings:
            d["t"] = self.timings
        return d


Approver = Callable[[dict[str, Any]], Awaitable[bool | None]]   # True approve, False deny, None unavailable


@dataclass
class _Cached:
    answers: dict[str, Any]
    unresolved: list[str]
    posting: Posting
    form: ApplicationForm
    expires: float
    adapter: Any = None
    summary: str = ""


@dataclass
class _Ctx:
    adapter: PlatformAdapter
    posting: Posting
    form: ApplicationForm
    summary: str = ""
    meta: dict[str, Any] = field(default_factory=dict)


class ApplyEngine:
    def __init__(self, settings: Settings, *, resolve_adapter: Callable[[str], PlatformAdapter],
                 profile: ProfileLike, router: RouterLike | None, gates: GatesLike, store: Store,
                 driver_factory: Callable[[str], BrowserDriver] | None = None,
                 control: ControlLike | None = None, approvals: ApprovalBook | None = None):
        self.s = settings
        self._resolve = resolve_adapter
        self.profile, self.router, self.gates, self.store = profile, router, gates, store
        self._driver_factory = driver_factory or self._default_driver
        self._drivers: dict[str, BrowserDriver | None] = {}
        self._plans: dict[str, _Cached] = {}
        self._locks: dict[str, asyncio.Lock] = {}
        self.control = control
        self.approvals = approvals or ApprovalBook(":memory:", ttl_s=settings.apply.approval_ttl_s)
        self._browser_sem = asyncio.Semaphore(max(1, settings.concurrency.browser_workers))
        self._plan_sem = asyncio.Semaphore(max(1, settings.apply.plan_workers))
        self.timings: dict[str, dict[str, int]] = {}
        self._exemplar: str | None = None

    # ---------------------------------------------------------------- helpers
    def _default_driver(self, name: str) -> BrowserDriver:
        import importlib

        from ..core import registry
        mod = {"playwright": "browser_playwright", "browser_use": "browser_use_driver"}.get(name)
        if mod:
            try:
                importlib.import_module(f"{__package__}.{mod}")
            except ImportError as e:
                raise ComponentMissing(f"browser driver '{name}' needs its extra installed: {e}") from e
        return registry.browser_drivers.create(name, settings=self.s)

    def _driver(self, name: str | None) -> BrowserDriver | None:
        if not name:
            return None
        if name not in self._drivers:
            try:
                self._drivers[name] = self._driver_factory(name)
            except (ComponentMissing, KeyError):
                self._drivers[name] = None
        return self._drivers[name]

    def _lock(self, company: str) -> asyncio.Lock:
        own = getattr(self.store, "company_lock", None)
        if own is not None:
            return own(company)
        return self._locks.setdefault(normalize(company) or "?", asyncio.Lock())

    @contextmanager
    def _stage(self, ref: str, name: str):
        t0 = time.monotonic()
        try:
            yield
        finally:
            d = self.timings.setdefault(ref, {})
            d[name] = d.get(name, 0) + int((time.monotonic() - t0) * 1000)

    async def _context(self, ref: str) -> _Ctx:
        adapter = self._resolve(ref)
        with self._stage(ref, "fetch"):  # posting + form concurrently
            posting, form = await asyncio.gather(adapter.get_posting(ref, with_description=True),
                                                 adapter.get_form(ref))
        return _Ctx(adapter, posting, form, summarize_posting(posting.description, self.s.apply.context_chars))

    def _stop_hits(self, form: ApplicationForm) -> list[str]:
        phrases = [p.lower() for p in self.s.policy.stop_phrases]
        return [f.label for f in form.fields if any(p in f.label.lower() for p in phrases)]

    async def _gate(self, ctx: _Ctx) -> GateResult:
        ref = ctx.form.ref
        with self._stage(ref, "gate"):  # policy gates and the dedupe lookup run concurrently
            gate, dup = await asyncio.gather(
                self.gates.evaluate(ctx.posting, ctx.form),
                self.store.seen(ctx.posting.company, ctx.posting.title, within_days=self.s.policy.dedupe_days,
                                ref=ref, url=ctx.posting.apply_url or ctx.posting.url))
        reasons = list(gate.reasons)
        decision = gate.decision
        hits = self._stop_hits(ctx.form)
        if hits and decision == Decision.submit:
            decision = Decision.needs_human
            reasons.append(f"attestation/policy field: {hits[0][:60]}")
        if dup and decision != Decision.skip:
            decision = Decision.skip
            reasons.append("duplicate")
        return GateResult(decision=decision, reasons=reasons)

    # ------------------------------------------------------------------- plan
    async def plan(self, ref: str) -> PlanOut:
        async with self._plan_sem:
            ctx = await self._context(ref)
            return await self._plan_ctx(ref, ctx)

    async def _plan_ctx(self, ref: str, ctx: _Ctx) -> PlanOut:
        gate = await self._gate(ctx)
        if gate.decision != Decision.submit:
            return PlanOut(plan=ApplicationPlan(ref=ref, gate=gate), timings=self.timings.get(ref, {}))
        audit: dict[str, Any] = {}
        sal_answers: list[Answer] = []
        sal_decision = compute_ask(self.s.policy, ctx.posting)
        rest: list[FormField] = []
        for f in ctx.form.fields:
            v = format_ask(f, sal_decision.ask) if sal_decision and is_salary_field(f) else None
            if sal_decision and is_salary_field(f) and v is not None:
                sal_answers.append(Answer(key=f.key, value=v, source="rule:salary"))
                audit.setdefault("salary", sal_decision.audit)
            else:
                rest.append(f)
        rest = [f for f in rest if not self._skip_cover(f)]
        with self._stage(ref, "map"):  # deterministic first
            mapped = map_fields(rest, self.profile, referral_preference=self.s.apply.referral_preference,
                                min_score=self.s.apply.option_min_score,
                                company=ctx.posting.company, top_languages=self.s.apply.top_languages)
        answers = sal_answers + list(mapped.answers)
        short = [f for f in mapped.unresolved if self._is_short(f) and self._llm_allowed(f)]
        long_ = [f for f in mapped.unresolved if f not in short]
        tasks: list[dict[str, Any]] = []
        if self.router is not None:
            # one batched short-field call and the prose pipeline run concurrently
            with self._stage(ref, "llm"):
                (got, leftover), (written, long_, wtasks) = await asyncio.gather(
                    self._llm_short(short, ctx), self._write_long(long_, ctx))
            answers += got
            long_ += leftover
            answers += written
            tasks += wtasks
        else:
            long_ += short
        unresolved_keys: list[str] = []
        have = {t["key"] for t in tasks}
        for f in long_:
            if f.key in have:
                continue
            if not f.required and not (f.kind == FieldKind.textarea and self.s.apply.write_optional):
                continue
            unresolved_keys.append(f.key)
            tasks.append(self._task(f, ctx))
        unresolved_keys = list(dict.fromkeys(unresolved_keys + [t["key"] for t in tasks]))
        planned = {a.key: a.value for a in answers}
        self._plans[ref] = _Cached(planned, unresolved_keys, ctx.posting, ctx.form,
                                   time.monotonic() + self.s.apply.plan_ttl_s, ctx.adapter, ctx.summary)
        _, files = self._split(ctx.form, planned)
        aid = self.approvals.issue(ref, self._url(ctx), ctx.posting.company, ctx.posting.title, planned, files)
        self.approvals.set_state(ref, "planned", company=ctx.posting.company, title=ctx.posting.title,
                                 approval_id=aid, url=self._url(ctx), only_from=("planned", "approved", "failed", "handoff"))
        return PlanOut(plan=ApplicationPlan(ref=ref, gate=gate, answers=answers, unresolved=unresolved_keys,
                                                audit=audit),
                       client_tasks=tasks, approval_id=aid, timings=self.timings.get(ref, {}))

    def _skip_cover(self, f: FormField) -> bool:
        """Cover letters only when the form requires one (writing.cover_letter_policy, ADR-0023)."""
        return ("cover" in normalize(f.label) and not f.required
                and self.s.writing.cover_letter_policy != "always")

    def _is_short(self, f: FormField) -> bool:
        if f.kind == FieldKind.file:
            return False
        if f.kind == FieldKind.textarea:
            return bool(f.max_length and f.max_length <= self.s.apply.short_text_max)
        return True

    @staticmethod
    def _llm_allowed(f: FormField) -> bool:
        return not (canonical_key(f.label) in _SENSITIVE_KEYS or _SENSITIVE_LABEL.search(f.label)
                    or _HUMAN_LABEL.search(f.label) or _NO_FABRICATE_LABEL.search(f.label)
                    or geo_kind(f) or f.kind == FieldKind.file)

    def _task(self, f: FormField, ctx: _Ctx, extra: dict[str, Any] | None = None) -> dict[str, Any]:
        t: dict[str, Any] = {"key": f.key, "label": f.label, "max_len": f.max_length or 0,
                             "context": {"role": f"{ctx.posting.title} @ {ctx.posting.company}",
                                         "posting": ctx.summary, "facts": self.profile.facts(f.label, 6)}}
        if f.options:
            t["options"] = f.options[:30]
        if _HUMAN_LABEL.search(f.label):
            t["needs_human"] = True
            t["reason"] = "attestation/AI-use question: no explicit profile answer"
        if extra:
            t.update(extra)
        return t

    async def _llm_short(self, fields: list[FormField], ctx: _Ctx) -> tuple[list[Answer], list[FormField]]:
        """ONE router call for every remaining short field; each reply is validated independently."""
        if not fields or self.router is None:
            return [], list(fields)
        spec = []
        for f in fields:
            row: dict[str, Any] = {"key": f.key, "q": f.label, "type": f.kind.value}
            if f.options:
                row["options"] = f.options[:40]
            if f.max_length:
                row["max_chars"] = f.max_length
            spec.append(row)
        facts = "\n".join(f"- {x}" for x in self.profile.facts(" ".join(f.label for f in fields), 14))
        prompt = (f"Fill application fields for {ctx.posting.title} at {ctx.posting.company}. Use ONLY these "
                  f"facts; when they do not determine an answer use null. For options answer with one option "
                  f"exactly.\nFACTS:\n{facts}\nFIELDS:\n{json.dumps(spec, separators=(',', ':'))}\n"
                  'Return one JSON object keyed by field key: {"<key>": {"value": ..., "confidence": 0..1}}.')
        schema = {"type": "object"}
        try:
            raw = await self.router.complete(prompt, task_class="short", max_tokens=80 + 60 * len(fields),
                                             json_schema=schema)
            m = re.search(r"\{.*\}", raw, re.DOTALL)
            data = json.loads(m.group(0)) if m else {}
        except Exception:  # noqa: BLE001 - a flaky model must never break planning
            return [], list(fields)
        out, left = [], []
        for f in fields:
            d = data.get(f.key) if isinstance(data, dict) else None
            ans = self._accept(f, d)
            (out if ans else left).append(ans or f)
        return out, left

    def _accept(self, f: FormField, d: Any) -> Answer | None:
        if not isinstance(d, dict):
            return None
        value = d.get("value")
        try:
            conf = float(d.get("confidence") or 0)
        except (TypeError, ValueError):
            return None
        if value in (None, "") or conf < self.s.apply.llm_min_confidence:
            return None
        if isinstance(value, str) and value.strip().lower() in ("null", "none", "n/a", "na", "unknown"):
            return None                                   # the model's "no answer" must never be typed into a form
        value = self._validate(f, value)
        if value is not None and not self._grounded(f, value):
            value = None
        return None if value is None else Answer(key=f.key, value=value, source="llm:short", confidence=conf)

    def _grounded(self, f: FormField, value: Any) -> bool:
        """The LLM may only restate profile facts: no placeholder/fabricated links, no unseen numbers, and no
        yes/no (or skill) claim about experience unless the profile facts mention the topic."""
        vals = value if isinstance(value, list) else [value]
        if any(looks_fabricated(v) for v in vals):
            return False
        facts = " ".join(self.profile.facts("", 60)).lower()
        ftoks = set(tokens(facts))
        raw = [str(v) for v in vals if not isinstance(v, bool)]
        for v in raw:  # urls / emails / phones / numbers must already exist in the profile
            for m in re.findall(r"https?://\S+|\S+@\S+|\+?\d[\d\-. ()]{5,}\d|\d+", v):
                if m.lower().strip(".,") not in facts and m not in f.label:
                    return False
        if _EXPERIENCE_LABEL.search(f.label):
            topic = {w for w in tokens(f.label) if w not in _GENERIC and len(w) > 2}
            claim = any(isinstance(v, bool) or polarity(str(v)) != 0 or str(v).strip().lower() in ("yes", "no")
                        for v in vals)
            if claim and not (topic and topic <= ftoks):
                return False
            if not claim and not all(set(tokens(x)) <= ftoks | _GENERIC for x in raw):
                return False
        return True

    @staticmethod
    def _validate(f: FormField, value: Any) -> Any | None:
        if f.kind == FieldKind.multiselect:
            vals = [o for o in (best_option(v, f.options) for v in (value if isinstance(value, list) else [value])) if o]
            return vals or None
        if f.kind == FieldKind.select or f.options:
            return best_option(value, f.options)
        if f.kind == FieldKind.boolean:
            return value if isinstance(value, bool) else {"yes": True, "no": False}.get(str(value).lower())
        if f.kind == FieldKind.number:
            try:
                return float(value) if "." in str(value) else int(value)
            except ValueError:
                return None
        s = str(value).strip()
        return s if not f.max_length or len(s) <= f.max_length else None

    async def _write_long(self, fields: list[FormField], ctx: _Ctx):
        """Prose only for required (or opted-in) long text; fields written concurrently."""
        written: list[Answer] = []
        rest: list[FormField] = []
        tasks: list[dict[str, Any]] = []
        if not fields or self.router is None or not self.s.writing.enabled:
            return written, list(fields), tasks
        if self._exemplar is None:
            self._exemplar = load_exemplar(self.s.writing.style_exemplar)
        avail = getattr(self.router, "available", None)
        todo: list[FormField] = []
        for f in fields:
            wanted = f.required or self.s.apply.write_optional or "cover" in normalize(f.label)
            if (f.kind == FieldKind.textarea and wanted and self._llm_allowed(f)
                    and not ("cover" in normalize(f.label) and self.s.writing.cover_letter_policy == "never")):
                todo.append(f)
            else:
                rest.append(f)
        if todo and avail is not None:
            try:
                if not await avail("narrative"):
                    return written, rest + todo, tasks
            except Exception:  # noqa: BLE001
                return written, rest + todo, tasks

        async def one(f: FormField):
            cover = "cover" in normalize(f.label)
            return await write_text(
                self.router, self.s.writing, kind="cover_letter" if cover else "free_text", label=f.label,  # type: ignore[arg-type]
                company=ctx.posting.company, title=ctx.posting.title, summary=ctx.summary,
                facts=self.profile.facts(f.label, 12), max_len=f.max_length, exemplar=self._exemplar or "",
                resume=getattr(self.profile, "resume_facts", None), corpus=getattr(self.profile, "corpus", None))

        with self._stage(ctx.form.ref, "write"):
            results = await bounded_gather((one(f) for f in todo), self.s.concurrency.global_limit)
        for f, res in zip(todo, results, strict=True):
            if isinstance(res, BaseException):
                rest.append(f)
            elif res.status == "pass":
                written.append(Answer(key=f.key, value=res.text, source="llm:narrative"))
            elif res.status == "needs_review":
                tasks.append(self._task(f, ctx, {"draft": res.text, "review": res.rubric, "scorecard": res.scorecard}))
            else:
                tasks.append(self._task(f, ctx, {"draft": res.text, "needs_human": True,
                                                 "violations": [v.line() for v in res.violations][:6],
                                                 "scorecard": res.scorecard}))
        return written, rest, tasks

    # ----------------------------------------------------------------- submit
    def _cached(self, ref: str) -> _Cached | None:
        c = self._plans.get(ref)
        if c and c.expires < time.monotonic():
            del self._plans[ref]
            return None
        return c

    @staticmethod
    def _url(ctx: _Ctx) -> str:
        return ctx.posting.apply_url or ctx.posting.url

    @staticmethod
    def _split(form: ApplicationForm, eff: dict[str, Any]) -> tuple[dict[str, Any], dict[str, str]]:
        kinds = {f.key: f.kind for f in form.fields}
        files = {k: str(v) for k, v in eff.items() if kinds.get(k) == FieldKind.file}
        return {k: v for k, v in eff.items() if k not in files}, files

    def _resolve_dry(self, dry_run: bool | None) -> bool:
        """A per-call dry_run can only make things safer (ADR-0021)."""
        if dry_run is False and self.s.dry_run:
            raise PermissionError("per-call dry_run=false refused: global dry_run is on; "
                                  "set dry_run=false in the user config to allow live submits")
        return self.s.dry_run if dry_run is None else dry_run

    def _state_block(self, ref: str) -> SubmitResult | None:
        cur = self.approvals.state(ref)
        if cur and cur["state"] in ("submitting", "unknown", "submitted", "awaiting_code"):
            ev = cur.get("evidence") or evidence_path(self.s.paths.data_dir, ref)
            return SubmitResult(ref=ref, status="needs_human" if cur["state"] != "submitted" else "duplicate",
                                detail=(f"submission state={cur['state']} (never auto-resubmitted); "
                                        f"detail={cur.get('detail')}; evidence={ev}; "
                                        "verify manually then call resolve_submission"
                                        + ("; a verification code is pending: provide_verification_code"
                                           if cur["state"] == "awaiting_code" else ""))[:300])
        return None

    async def _authorize(self, ref: str, ctx: _Ctx, eff: dict[str, Any], approval_id: str | None,
                         approver: Approver | None, cached: bool) -> tuple[str | None, str, str]:
        """Return (approval_id, error, approved_by). Server-held records only; nothing client-computable."""
        auto = self.s.apply.auto_approve_active
        company, title = ctx.posting.company, ctx.posting.title
        _, files = self._split(ctx.form, eff)
        rec, err = self.approvals.check(approval_id, ref, self._url(ctx)) if approval_id else (None, "")
        if rec is None and auto:
            aid = self.approvals.issue(ref, self._url(ctx), company, title, eff, files)
            self.approvals.approve(aid, "auto_approve")
            return aid, "", "auto_approve"
        if rec is None:
            return None, err or "missing approval_id; call plan_application and pass its approval_id", ""
        if not self.approvals.matches(rec, eff, files):
            if not cached:
                return None, "answers do not match the approved plan (no cached plan); re-plan", ""
            rec = self.approvals.rebind(rec, company, title, eff, files)
        if self.s.apply.approval_mode == "agent":
            self.approvals.approve(rec.approval_id, "agent")
            return rec.approval_id, "", "agent"
        if rec.status != "approved":                    # human mode: needs a separate human channel
            from ..grants import try_use  # human-signed time-limited grant (ADR-0027)
            g = try_use(self.s, "approve_submissions", f"ref={ref} approval={rec.approval_id[:8]}")
            if g is not None:
                self.approvals.approve(rec.approval_id, f"grant:{g.id}")
                return rec.approval_id, "", f"grant:{g.id}"
            verdict = await approver(rec.summary) if approver else None
            if verdict is True:
                self.approvals.approve(rec.approval_id, "human:elicit")
            elif verdict is False:
                self.approvals.deny(rec.approval_id, "human:elicit")
                return None, "human denied the submission", ""
            else:
                return None, (f"awaiting human approval: run `job-apply-mcp approve {rec.approval_id}` "
                              f"(then retry with the same answers)"), ""
        return rec.approval_id, "", self.approvals.get(rec.approval_id).approved_by or "human"  # type: ignore[union-attr]

    def _submission_state(self, ref: str) -> str | None:
        st = self.approvals.state(ref) if self.approvals is not None else None
        return st["state"] if st else None

    async def _release_unsent(self, ref: str, reason: str, event_id: str = "") -> None:
        """Return the control quota of a live submit whose state proves nothing was sent (ADR-0032)."""
        release = getattr(self.control, "release", None)
        if release is None or self._submission_state(ref) not in _NOT_SENT:
            return
        with contextlib.suppress(Exception):   # a failed release only keeps the count higher (fail closed)
            await (release(ref, reason, event_id) if event_id else release(ref, reason))

    async def _control(self, action: str, ctx: _Ctx, dry: bool, eff: dict[str, Any] | None = None) -> str | None:
        """Optional control layer (ADR-0014). Returns a block reason, or None when allowed."""
        if self.control is None:
            return None
        res = await self.control.check(action, {"ref": ctx.form.ref, "company": ctx.posting.company,
                                                "title": ctx.posting.title, "dry_run": dry,
                                                "platform": ctx.posting.platform, "answers": eff or {},
                                                "submission_state": self._submission_state(ctx.form.ref)})
        verdict = res
        for attr in ("decision", "verdict", "status", "allowed", "ok"):
            v = res.get(attr) if isinstance(res, dict) else getattr(res, attr, None)
            if v is not None:
                verdict = v
                break
        ctx.meta["control_event_id"] = (res.get("event_id") if isinstance(res, dict)
                                        else getattr(res, "event_id", "")) or ""
        v = getattr(verdict, "value", verdict)
        if v is None or v is True or str(v).lower() in ("allow", "allowed", "ok", "pass", "submit", "proceed"):
            return None
        reasons = res.get("reasons") if isinstance(res, dict) else getattr(res, "reasons", None)
        return f"control: {v}" + (f" ({'; '.join(map(str, reasons))[:120]})" if reasons else "")

    async def submit(self, ref: str, answers: dict[str, Any] | None = None, approval_id: str | None = None,
                     dry_run: bool | None = None, approver: Approver | None = None) -> SubmitResult:
        try:
            dry = self._resolve_dry(dry_run)
        except PermissionError as e:
            return SubmitResult(ref=ref, status="error", detail=str(e))
        supplied = dict(answers or {})
        try:
            if not dry:
                await self.expire_pending()
                blocked = self._state_block(ref)
                if blocked:
                    return blocked
            cached = self._cached(ref)
            if cached and cached.adapter is not None:
                ctx = _Ctx(cached.adapter, cached.posting, cached.form, cached.summary)  # no refetch
            else:
                ctx = await self._context(ref)
            if not dry and not self.s.apply.auto_approve_active:
                _, err = self.approvals.check(approval_id, ref, self._url(ctx))
                if err:
                    return SubmitResult(ref=ref, status="error", detail=err)
            if cached:
                for k, v in supplied.items():
                    if k in cached.answers and cached.answers[k] != v and not dry:
                        return SubmitResult(ref=ref, status="error",
                                            detail=f"answer '{k}' differs from the approved plan; re-plan")
                eff = {**cached.answers, **supplied}
            elif dry or self.s.apply.auto_approve_active:
                plan = await self._plan_ctx(ref, ctx)
                if plan.plan.gate.decision != Decision.submit:
                    return SubmitResult(ref=ref, status="duplicate" if "duplicate" in plan.plan.gate.reasons
                                        else "needs_human", detail="; ".join(plan.plan.gate.reasons)[:200])
                eff = {**{a.key: a.value for a in plan.plan.answers}, **supplied}
            else:
                eff = supplied                      # stateless: must hash-match the server-held record
            hits = self._stop_hits(ctx.form)
            if hits:
                return SubmitResult(ref=ref, status="needs_human", detail=f"attestation field: {hits[0][:60]}")
            missing = [f.key for f in ctx.form.fields if f.required and f.key not in eff and not geo_kind(f)]
            if missing:
                return SubmitResult(ref=ref, status="needs_human", detail=f"unresolved required: {missing[:8]}")
            blocked = await self._control("submit", ctx, dry, eff)
            if blocked:
                return SubmitResult(ref=ref, status="needs_human", detail=blocked)
            by = ""
            if not dry:
                approval_id, err, by = await self._authorize(ref, ctx, eff, approval_id, approver, bool(cached))
                if err:
                    return SubmitResult(ref=ref, status="needs_human" if err.startswith("awaiting") else "error",
                                        detail=err)
            async with self._lock(ctx.posting.company):
                why = None
                if hasattr(self.store, "seen_why"):
                    why = await self.store.seen_why(ctx.posting.company, ctx.posting.title,
                                                    within_days=self.s.policy.dedupe_days, ref=ref,
                                                    url=ctx.posting.apply_url or ctx.posting.url)
                elif await self.store.seen(ctx.posting.company, ctx.posting.title,
                                           within_days=self.s.policy.dedupe_days, ref=ref,
                                           url=ctx.posting.apply_url or ctx.posting.url):
                    why = "seen"
                if why:
                    return SubmitResult(ref=ref, status="duplicate", detail=f"already applied ({why})")
                if not dry:
                    self.approvals.audit("submit_attempt", ref=ref, approval_id=approval_id, actor=by,
                                         detail=f"{ctx.posting.company} | {ctx.posting.title}")
                    ok, st = self.approvals.begin_submit(ref, approval_id)   # durable BEFORE the click
                    if not ok:
                        return self._state_block(ref) or SubmitResult(ref=ref, status="needs_human", detail=st)
                try:
                    with self._stage(ref, "submit"):
                        result = await self._dispatch(ctx, eff, dry)
                except BaseException as e:  # incl. cancellation: the click may have happened
                    if not dry:
                        st = "unknown" if ctx.meta.get("dispatched") else "failed"   # pre-dispatch is safe to retry
                        self.approvals.finish(ref, st, f"{type(e).__name__}: {e}"[:200],
                                              evidence_path(self.s.paths.data_dir, ref))
                        if isinstance(e, Exception):
                            await self._release_unsent(ref, f"{type(e).__name__} before dispatch", ctx.meta.get("control_event_id", ""))
                    raise
                if not dry:
                    st = {"submitted": "submitted", "roadblock": "unknown", "needs_human": "handoff",   # roadblock may follow the click
                          "duplicate": "failed", "awaiting_code": "awaiting_code",
                          "incomplete": "handoff"}.get(result.status, "unknown")
                    if result.status == "error" and not ctx.meta.get("dispatched"):
                        st = "failed"
                    self.approvals.finish(ref, st, f"{result.status}: {result.detail or ''}",
                                          evidence_path(self.s.paths.data_dir, ref))
                    await self._release_unsent(ref, f"not sent: {result.status}", ctx.meta.get("control_event_id", ""))
                if result.status in ("submitted", "needs_human", "roadblock", "incomplete"):
                    await self.store.record(result, company=ctx.posting.company, title=ctx.posting.title,
                                            url=ctx.posting.apply_url or ctx.posting.url)
                if result.status == "submitted":
                    self._plans.pop(ref, None)
                return result
        except ComponentMissing:
            raise
        except Exception as e:  # noqa: BLE001
            return SubmitResult(ref=ref, status="error", detail=f"{type(e).__name__}: {e}"[:200])

    # ------------------------------------------------ human-provided verification codes (ADR-0026)
    def _code_drivers(self) -> list[Any]:
        return [d for d in self._drivers.values() if d is not None and hasattr(d, "provide_code")]

    async def _apply_code_result(self, res: SubmitResult) -> SubmitResult:
        """Persist the outcome of a code step. Never a second submit: an unclear outcome stays blocking."""
        ref = res.ref
        st = {"submitted": "submitted", "awaiting_code": "awaiting_code", "duplicate": "failed",
              "roadblock": "unknown", "handoff": "unknown"}.get(res.status, "unknown")
        if res.status == "handoff":
            res = res.model_copy(update={"detail": f"handoff: {res.detail or ''}"[:400]})
        self.approvals.finish(ref, st, f"{res.status}: {res.detail or ''}",
                              evidence_path(self.s.paths.data_dir, ref))
        if res.status == "submitted":
            cur = self.approvals.state(ref) or {}
            await self.store.record(res, company=cur.get("company", ""), title=cur.get("title", ""))
            self._plans.pop(ref, None)
        return res

    async def expire_pending(self) -> list[SubmitResult]:
        """Move every code prompt past its window to a handoff (state `unknown`: blocking, human-resolvable)."""
        out: list[SubmitResult] = []
        for d in self._code_drivers():
            for r in await d.expire_pending():
                self.approvals.audit("code_expired", ref=r.ref, actor="server")
                out.append(await self._apply_code_result(r))
        return out

    async def provide_verification_code(self, pending_id: str, code: str) -> SubmitResult:
        """The USER typed the code they received; this only validates its format, types it, clicks the page's
        verify button once and runs normal confirmation detection. Never reads mail; never logs/stores the code."""
        await self.expire_pending()
        drv = next((d for d in self._code_drivers() if d.pending_info(pending_id)), None)
        if drv is None:
            return SubmitResult(ref="", status="error", detail="unknown or expired pending_id")
        info = drv.pending_info(pending_id)
        ref = info["ref"]
        if (err := drv.pending_check(pending_id, code)):             # format only; costs no attempt
            return SubmitResult(ref=ref, status="awaiting_code", pending_id=pending_id,
                                detail=f"invalid code format: {err}. Not entered; ask the user to re-check it.")
        if not self.approvals.set_state(ref, "submitting", detail="verification code entered by the user",
                                        only_from=("awaiting_code",)):
            return SubmitResult(ref=ref, status="error", detail="submission is not awaiting a code")
        self.approvals.audit("code_provided", ref=ref, actor="user", detail="code entered (value not recorded)")
        try:
            res = await drv.provide_code(pending_id, code)
        except BaseException:
            self.approvals.finish(ref, "unknown", "code entry interrupted; verify manually")
            raise
        if res is None:
            res = SubmitResult(ref=ref, status="error", detail="unknown or expired pending_id")
        return await self._apply_code_result(res)

    async def resolve_submission(self, ref: str, outcome: str = "", note: str = "",
                                 actor: str = "agent", evidence: str = "") -> dict[str, Any]:
        """Inspect (no outcome) or resolve a ref stuck in submitting/unknown. Never auto-called.
        The agent path exists only in approval_mode="agent" and must cite the ref's evidence folder."""
        if outcome and actor == "agent":
            ev = evidence_path(self.s.paths.data_dir, ref)
            if self.s.apply.approval_mode != "agent":
                self.approvals.audit("resolve_refused", ref=ref, actor="agent", detail="human mode")
                return {"ref": ref, "error": "resolve_submission is human-only (CLI) in approval_mode=human"}
            if not ev or evidence != ev:
                self.approvals.audit("resolve_refused", ref=ref, actor="agent", detail=f"bad evidence {evidence!r}")
                return {"ref": ref, "error": "evidence must be the ref's evidence folder", "evidence": ev}
            note = f"{note} [evidence={evidence}]"
        if outcome:
            ok, msg = self.approvals.resolve(ref, outcome, note, actor)
            cur = self.approvals.state(ref)
            if ok and outcome == "submitted" and cur:
                await self.store.record(SubmitResult(ref=ref, status="submitted", detail=f"resolved by {actor}"),
                                        company=cur["company"], title=cur["title"])
            if not ok:
                return {"ref": ref, "error": msg}
        cur = self.approvals.state(ref) or {}
        return {"ref": ref, "state": cur.get("state"), "detail": cur.get("detail"), "idem": cur.get("idem_key"),
                "evidence": cur.get("evidence") or evidence_path(self.s.paths.data_dir, ref)}

    async def _dispatch(self, ctx: _Ctx, eff: dict[str, Any], dry: bool) -> SubmitResult:
        form, ref = ctx.form, ctx.form.ref
        values, files = self._split(form, eff)
        last = SubmitResult(ref=ref, status="needs_human", detail="no submission path available")
        for path in form.submit_via or ["api", "browser"]:
            if path == "api":
                ctx.meta["dispatched"] = True
                res = await ctx.adapter.submit(form, values, files, dry_run=dry)
                if res.status != "needs_browser":
                    return res
                last = res
            elif path == "browser":
                res = await self._browser(ctx, values, files, dry)
                if res is not None:
                    return res
        return SubmitResult(ref=ref, status="needs_human", detail=(last.detail or last.status)[:200])

    async def _browser(self, ctx: _Ctx, values: dict[str, Any], files: dict[str, str],
                       dry: bool) -> SubmitResult | None:
        url = ctx.posting.apply_url or ctx.posting.url
        last: SubmitResult | None = None
        for name in (self.s.browser.driver, self.s.browser.fallback_driver):
            drv = self._driver(name)
            if drv is None:
                continue
            demo = {f"demographic:{k}": v for k in DEMOGRAPHIC_KEYS if (v := self.profile.lookup(k))}
            rows = getattr(getattr(self.profile, "p", None), "education_rows", list)()
            edu = {f"education:{i}": r for i, r in enumerate(rows[:1])}
            async with self._browser_sem:
                ctx.meta["dispatched"] = True
                last = await drv.fill_and_submit(url, ctx.form, {**values, **demo, **edu}, files, dry_run=dry)
            if not (last.status == "needs_human" and (last.detail or "").startswith("unsupported_site")):
                return last
        return last or SubmitResult(ref=ctx.form.ref, status="needs_human",
                                    detail="no browser driver available (install extra 'browser')")

    # ------------------------------------------------------------------ batch
    async def apply_batch(self, refs: list[str], dry_run: bool | None = None,
                          max_parallel: int | None = None) -> list[dict[str, Any]]:
        """Pipelined: planning workers (apply.plan_workers) and browser workers (concurrency.browser_workers)
        are separate pools, so job N+1 is planned (and its prose written) while job N is in the browser."""
        try:
            dry = self._resolve_dry(dry_run)
        except PermissionError as e:
            return [{"ref": r, "status": "error", "detail": str(e)} for r in refs]
        browser_cap = asyncio.Semaphore(max_parallel or self.s.concurrency.browser_workers)

        async def one(ref: str) -> dict[str, Any]:
            out = await self.plan(ref)
            g = out.plan.gate
            if g.decision != Decision.submit:
                return {"ref": ref, "status": g.decision.value, "why": g.reasons, "t": out.timings}
            if out.plan.unresolved:
                human = any(t.get("needs_human") for t in out.client_tasks)
                return {"ref": ref, "status": "needs_human" if human else "needs_input", "unresolved": out.plan.unresolved,
                        "client_tasks": out.client_tasks, "approval_id": out.approval_id, "t": out.timings}
            if not dry and not self.s.apply.auto_approve_active:
                return {"ref": ref, "status": "awaiting_approval", "approval_id": out.approval_id, "t": out.timings}
            async with browser_cap:
                r = await self.submit(ref, {}, out.approval_id, dry)
            row = {"ref": ref, "status": r.status, "via": r.via, "detail": r.detail, "pending_id": r.pending_id,
                   "t": self.timings.get(ref, {})}
            return {k: v for k, v in row.items() if v}

        # every ref starts at once; the pools above (plan_sem, browser_cap/_browser_sem) do the limiting
        rows = await bounded_gather((one(r) for r in refs), max(len(refs), 1))
        return [r if not isinstance(r, BaseException) else {"status": "error", "detail": str(r)[:160]}
                for r in rows]

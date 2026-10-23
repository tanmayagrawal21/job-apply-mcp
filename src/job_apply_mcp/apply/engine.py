"""Apply engine (ADR-0011, ADR-0021): plan -> server-held approval -> submit (state machine), plus a batch.

Collaborators are injected and typed by the Protocols in ports.py / core/interfaces.py, so the engine is
testable with small fakes and independent of how profile, router, gates and store are implemented.
"""
from __future__ import annotations

import asyncio
import contextlib
import contextvars
import hashlib
import json
import re
import time
from collections.abc import Awaitable, Callable
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

from pydantic import BaseModel, Field

from ..config import Settings
from ..consent.otp import OTP_SETUP_HINT
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
from ..llm.base import LLM_TIMINGS
from ..writing import checks
from . import autonomy
from .approvals import ApprovalBook, evidence_path
from .autonomy import Assumption
from .mapper import (
    DEMOGRAPHIC_KEYS,
    SEMANTIC_SENSITIVE,
    best_option,
    canonical_key,
    contact_permission_channels,
    eeo_class,
    geo_kind,
    job_country,
    llm_safe_short,
    looks_fabricated,
    map_fields,
    never_llm,
    normalize,
    polarity,
    tokens,
)
from .ports import ComponentMissing, ControlLike, GatesLike, ProfileLike, RouterLike
from .salary import compute_ask, format_ask, is_salary_field
from .writing import WriteProgress, load_exemplar, review_rubric, review_text, summarize_posting, write_text

# Fields the local LLM must never guess: a wrong answer is a false legal/biographical statement.
_SENSITIVE_KEYS = SEMANTIC_SENSITIVE | {"work_authorized", "requires_sponsorship", "gender", "hispanic", "race", "veteran",
                   "disability", "over_18", "privacy_ack", "salary_expectation", "start_date", "graduation",
                   "pronouns", "sexual_orientation"}
_SENSITIVE_LABEL = re.compile(r"how many years|years of|criminal|convict|felony|clearance|citizen|"
                              r"agree|certify|attest|signature|ssn|social security|graduat|\bdate\b|semester|\bterm\b|"
                              r"quarter|pronoun|gender|ethnic|\brace\b|veteran|disabilit", re.IGNORECASE)
# Attestations and AI-use questions: answered only from an explicit profile [answers] pattern, else a human.
_ATTEST_LABEL = re.compile(
    r"certif|attest|foreign talent|own work|by selecting|i agree|i acknowledge|i declare|under penalty|"
    r"true and (?:correct|accurate)|truthful|export control|conflict of interest", re.IGNORECASE)
# Attestation, certification, no-AI and consent wording that must never ride along inside an answer (ADR-0044).
_ATTEST_TEXT = re.compile(
    r"\b(?:i|we|the\s+applicant|applicant|the\s+candidate|candidate|the\s+undersigned)\s+"
    r"(?:hereby\s+|also\s+|further\s+|have\s+|has\s+|do\s+|did\s+|can\s+|will\s+)?"
    r"(?:certif(?:y|ies|ied)|attest(?:s|ed)?|affirm(?:s|ed)?|declar(?:e|es|ed)|confirm(?:s|ed)?|"
    r"acknowledg(?:e|es|ed)|agree(?:s|d)?|consent(?:s|ed)?|authori[sz](?:e|es|ed))\b|"
    r"\bhereby\b|\bpersonally\s+(?:completed|wrote|written|prepared)\b|"
    r"\b(?:completed|written|prepared)\s+by\s+me\s+personally\b|"
    r"\bwithout\s+(?:the\s+)?(?:use|help|assistance|aid)\s+of\s+(?:any\s+)?(?:an?\s+)?(?:ai|artificial intelligence|"
    r"llms?|chatgpt|generative)\b|\b(?:did\s+not|didn\W?t|never)\s+use(?:d)?\s+(?:any\s+|an?\s+)?(?:chatgpt|copilot|"
    r"ai|artificial intelligence|generative|llms?)\b|\bno\s+(?:generative\s+)?(?:ai|artificial intelligence)\b|"
    r"\bwithout\s+(?:any\s+|the\s+)?(?:ai|a\.i\.|artificial intelligence|llms?|chatgpt|generative)\b|"
    r"\b(?:ai|artificial intelligence|chatgpt|llms?)\s+(?:was|were|is|are|has\s+been|had\s+been)\s+(?:not|never)\s+(?:used|involved)\b|"
    r"\b(?:ai|llm)\W?free\b|\bzero\s+(?:ai|artificial intelligence|llms?)\b|\bsolemnly\b|\bswear\b|\bsworn\b|"
    r"\b(?:written|wrote|authored|composed|drafted|completed|prepared)\s+(?:this\s+|it\s+|all\s+)?by\s+me\b|"
    r"\bby\s+myself\b|\b(?:wrote|written|authored|composed|drafted)\s+(?:this|it|these)\s+myself\b|"
    r"\bunder\s+penalty\b|\btrue,?\s+(?:complete\s+)?and\s+(?:correct|accurate|complete)\b|\b(?:consent|agree)\s+to\b|"
    r"\bpermission\s+to\s+(?:text|call|contact|email|e\W?mail|message|sms)\b|\bsms\b|\btexts\b|"
    r"\btext\s+(?:me|messages?|messaging|updates?|alerts?)\b|\bcall\s+or\s+text\b|"
    r"\bcontact\s+me\s+(?:by|via|on|at|through)\s+(?:phone|telephone|text|sms|mobile|cell)\b|"
    # authorship and no-AI claims by meaning (ADR-0044)
    r"\bnot\s+(?:ai|llm|machine|computer)\W?(?:generated|written|assisted|produced)\b|\bnon\W?ai\b|"
    r"\bhuman\W?(?:written|authored|made|generated|crafted)\b|\b100\s?%\s+human\b|\bentirely\s+human\b|"
    r"\bmy\s+own\s+(?:words|writing|work)\b|\bevery\s+(?:single\s+)?word\b[^.!?\n]{0,60}\bmy\s+own\b|"
    r"\bentirely\s+(?:my\s+own|mine)\b|\bunaided\b|"
    r"\b(?:no|without(?:\s+any)?)\s+(?:outside\s+|external\s+)?(?:assistance|help)\b|"
    r"\b(?:wrote|written|authored|composed|drafted)\s+(?:this|it|these|them|everything)?\s*"
    r"(?:entirely\s+|completely\s+)?(?:alone|by\s+myself|myself|on\s+my\s+own|single\W?handedly)\b|"
    r"\bhand\W?written\s+by\b|"
    r"\b(?:did\s+not|didn\W?t|never|do\s+not|don\W?t|have\s+not|haven\W?t)\s+(?:use|used|rely\s+on|relied\s+on|consult|"
    r"consulted)\s+(?:any\s+|an?\s+)?(?:gpt|chatgpt|claude|gemini|copilot|bard|openai|llms?|ai|artificial intelligence)\b|"
    r"\b(?:gpt|chatgpt|claude|gemini|copilot|bard|openai|llms?|ai)(?:\W?\d+(?:\.\d+)?)?\s+(?:was|were|is|has\s+been|had\s+been)"
    r"\s+(?:not|never)\s+(?:used|involved|consulted|employed)\b|"
    # pledges, completeness statements and other consent/contact wording
    r"\b(?:i|we)\s+(?:hereby\s+)?(?:pledge|vouch|guarantee|warrant|promise)\b|"
    r"\b(?:accurate|true|correct)\s+and\s+(?:complete|accurate|correct|true)\b|"
    r"\baccept\s+the\s+(?:privacy|terms|policy|conditions)\b|\bprivacy\s+(?:policy|notice)\b|"
    r"\bopt\W?in\s+to\b|\bopt\s+me\s+in\b|\bsign\s+me\s+up\b|\bsubscribe\s+me\b|"
    r"\b(?:okay|ok|fine|happy|free|welcome)\s+to\s+(?:email|e\W?mail|call|ring|phone|text|contact|message)\s+me\b|"
    r"\b(?:receive|get|send\s+me|email\s+me)\s+(?:\w+\s+){0,3}?(?:promotions?|promotional|marketing|offers|newsletters?)\b|"
    # meaning-level paraphrases (general wording, any locale)
    r"\b(?:ai|llms?|chatgpt|generative\s+ai|language\s+models?)\s+(?:involvement|usage|use|assistance)\s*[:=-]\s*"
    r"(?:none|nil|no|zero|0|n/?a)\b|\buse\s+of\s+(?:ai|llms?|language\s+models?)\s*[:=-]\s*(?:none|nil|no|zero|0)\b|"
    r"\b(?:ai|llms?)\s*[:=-]\s*(?:none|nil|zero|0)\b|\bno\s+generative\s+tools?\b|"
    r"\bzero\s+use\s+of\s+(?:ai|llms?|language\s+models?)\b|\bnot\s+(?:generated|written|produced)\s+by\s+(?:a\s+)?"
    r"(?:machine|computer|bot|model|ai)\b|\bsole\s+(?:author|writer|creator)\b|\b(?:penned|authored)\s+by\s+me\b|"
    r"\b(?:authentically|genuinely|wholly|truly)\s+(?:mine|my\s+own)\b|\bon\s+my\s+(?:honou?r|word)\b|"
    r"\b(?:i\s+)?give\s+(?:you\s+)?my\s+word\b|\bfalse\s+(?:statements?|information)\s+(?:may|will|could|can)\b|"
    r"\b(?:reach|contact|message)\s+me\s+(?:by|via|on|at|through)\s+(?:phone|telephone|mobile|cell|whatsapp|"
    r"telegram|signal|viber|wechat|line|sms|text)\b",
    re.IGNORECASE)
# Text aimed at the reviewer model rather than the employer (ADR-0044).
_INJECT_TEXT = re.compile(
    r"[{\[]\s*\"?\s*(?:pass|violations|scores|at_least_as_strong|corrected)\b|(?:^|[\n.!?]\s*)(?:dear\s+)?(?:reviewer|"
    r"grader|evaluator|assistant|system|note to (?:the\s+)?(?:reviewer|model|ai))\s*[:,]|\bignore\s+(?:all\s+|any\s+|the\s+)?(?:previous|prior|above|earlier)\s+"
    r"(?:instructions|rules|text)|\b(?:respond|reply|answer)\s+with\b|\breturn\s+json\b|\bpass\s*[:=]\s*true\b|"
    r"\bevaluator\b|\brubric\b|</?\s*(?:pass|verdict|score)\s*>|\boutput\s+(?:a\s+)?[\"']?(?:pass|true)\b|"
    r"\bignore\s+(?:the\s+|all\s+|any\s+|your\s+)?(?:rubric|rules|instructions|criteria|checks?|guidelines|above|"
    r"previous|prior)\b|<<<|>>>",
    re.IGNORECASE)
# Consent / opt-in labels: like attestations, never set by the client in agent mode (ADR-0044).
# ADR-0050: plus every contact-permission question (mapper.contact_permission_channels), whatever its wording
_CONSENT_LABEL = re.compile(r"consent|\bsms\b|text messag|\btext(?:ed|ing)? (?:you|your|me)\b|\ba text\b|"
                            r"\b(?:call|ring) or text\b|whats ?app|marketing|opt[\s-]?in|subscribe|newsletter|"
                            r"terms (?:and|&) conditions|i agree|i accept", re.IGNORECASE)
_EXACT_VALUE = re.compile(
    r"(?:https?://|www\.)\S+|[+-]?\d[\d,]*(?:\.\d+)?\s?%?|\d{4}-\d{2}(?:-\d{2})?|\d{1,2}/\d{1,2}/\d{2,4}|"
    r"(?:jan|feb|mar|apr|may|jun|jul|aug|sep|sept|oct|nov|dec)[a-z]*\.?\s+(?:\d{1,2},?\s+)?\d{4}", re.IGNORECASE)
_FREE_TEXT_KINDS = frozenset({FieldKind.text, FieldKind.textarea, FieldKind.unknown})
_AI_LABEL = re.compile(r"\bai\b|artificial intelligence|\bllms?\b|chatgpt|copilot|generative", re.IGNORECASE)
_HUMAN_LABEL = re.compile(_ATTEST_LABEL.pattern + "|" + _AI_LABEL.pattern, re.IGNORECASE)
# "What AI tools are you using?" is a factual prose question (writer + review), not an AI-use attestation (ADR-0033).
_AI_TOOLS_PROSE = re.compile(r"^\W*(what|which)\b.*\bai\b.*\b(tools?|assistants?|workflows?)\b", re.IGNORECASE)
_PROSE_Q = re.compile(r"^\W*(why|what|how|describe|tell (?:us|me)|explain|share|walk us|please (?:describe|explain|share|tell|provide))\b",
                      re.IGNORECASE)
_NOT_PROSE_Q = re.compile(r"^\W*what (?:is|are) (?:your|the)\b|how many|how did you (?:hear|find)|how long|how soon|how much|"
                          r"\b(?:url|link|date|salary|compensation|phone|email|address|zip|postal|city|state|country|"
                          r"number|name|pronouns?|gender|age|gpa|school|degree|location|office|timezone)\b", re.IGNORECASE)
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
    skipped_voluntary: dict[str, str] = Field(default_factory=dict)   # key -> label (ADR-0040)
    profile_gaps: list[dict[str, Any]] = Field(default_factory=list)  # per-country facts to ask the owner (ADR-0040)

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
        if self.skipped_voluntary:
            d["skipped_voluntary"] = self.skipped_voluntary
        if self.profile_gaps:
            d["profile_gaps"] = self.profile_gaps
        assumed = {a.key: {"value": a.value, "rule": a.source, **({"why": a.rationale} if a.rationale else {}),
                           **({"confidence": a.confidence} if a.source.startswith("assumed:infer") else {})}
                   for a in self.plan.answers if a.assumed}
        if assumed:
            d["assumed"] = assumed         # ADR-0052: answers the agent assumed, never facts the owner gave
        if self.approval_id:
            d["approval_id"] = self.approval_id
        if self.timings:
            d["t"] = self.timings
        return d


# Per-task override of writing.mode (ADR-0045): batch runs plan with "deferred" without mutating settings.
WRITE_MODE: contextvars.ContextVar[str | None] = contextvars.ContextVar("jam_write_mode", default=None)


def _write_mode(settings: Settings) -> str:
    return WRITE_MODE.get() or settings.writing.mode


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
    gate: GateResult | None = None                                  # for draft_answers (ADR-0041)
    plan_answers: list[Answer] = field(default_factory=list)
    audit: dict[str, Any] = field(default_factory=dict)
    extra: dict[str, Any] = field(default_factory=dict)            # ADR-0040 hints / gaps kept for draft_answers


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
        self._otp_locks: dict[str, asyncio.Lock] = {}   # ADR-0048: one unattended code flow per sender domain
        self.control = control
        self.approvals = approvals or ApprovalBook(":memory:", ttl_s=settings.apply.approval_ttl_s)
        self._browser_sem = asyncio.Semaphore(max(1, settings.concurrency.browser_workers))
        self._plan_sem = asyncio.Semaphore(max(1, settings.apply.plan_workers))
        self._plan_sem_deferred = asyncio.Semaphore(max(1, settings.apply.plan_workers_deferred))
        self.timings: dict[str, dict[str, int]] = {}
        self._exemplar: str | None = None
        self.persona: Any = None      # PersonaPolicy (ADR-0047); set by the factory
        self.gaps: Any = None         # ProfileDB gap collector; set by the factory
        self._reviewed: set[str] = set()     # digests of (ref, key, text) client prose that passed review (ADR-0044)

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
        """Labels of fields whose label or options contain a stop phrase as whole words (ADR-0040)."""
        from ..policy.gates import stop_phrase_hit
        phrases = list(self.s.policy.stop_phrases)
        return [f.label for f in form.fields
                if any(stop_phrase_hit(t, phrases) for t in (f.label, *f.options))]

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
        # deferred prose makes planning fetch + mapping + one small batched call, so more may run at once (ADR-0045)
        sem = self._plan_sem_deferred if _write_mode(self.s) == "deferred" else self._plan_sem
        async with sem:
            ctx = await self._context(ref)
            return await self._plan_ctx(ref, ctx)

    @contextmanager
    def _llm_timings(self, ref: str):
        """Collect the router's queue-wait / generation ms for this plan into timings (ADR-0041)."""
        sink: dict[str, int] = {}
        tok = LLM_TIMINGS.set(sink)
        try:
            yield
        finally:
            LLM_TIMINGS.reset(tok)
            d = self.timings.setdefault(ref, {})
            for k, v in sink.items():
                d[k] = d.get(k, 0) + v

    async def _plan_ctx(self, ref: str, ctx: _Ctx) -> PlanOut:
        from ..persona import posting_scope
        with self._llm_timings(ref), posting_scope(ctx.posting.company, ctx.posting.title, ctx.summary):
            out = await self._plan_inner(ref, ctx)
        out.timings = dict(self.timings.get(ref, {}))
        return out

    async def _plan_inner(self, ref: str, ctx: _Ctx) -> PlanOut:
        gate = await self._gate(ctx)
        if gate.decision != Decision.submit:
            return PlanOut(plan=ApplicationPlan(ref=ref, gate=gate), timings=self.timings.get(ref, {}))
        audit: dict[str, Any] = {}
        sal_answers: list[Answer] = []
        sal_decision = self._salary(ctx.posting)
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
                                company=ctx.posting.company, top_languages=self.s.apply.top_languages,
                                stop_phrases=self.s.policy.stop_phrases,
                                company_aliases=self._company_aliases(ctx.posting.company), persona=self.persona,
                                job_country=job_country(ctx.posting.location, ctx.posting.countries,
                                                        ctx.posting.locations))
        answers = sal_answers + list(mapped.answers)
        # ADR-0040: optional self-identification questions stay blank and are listed, never sent to an LLM
        voluntary = {f.key: f.label[:80] for f in mapped.skipped_voluntary}
        pending = [f for f in mapped.unresolved if f.key not in voluntary]
        owner_only = [f for f in pending if never_llm(f)]        # legal status / place: never a model (ADR-0040)
        pending = [f for f in pending if f not in owner_only]
        # ADR-0040: the short-answer model only sees allowlisted questions; _llm_allowed is defence in depth
        short = [f for f in pending if self._is_short(f) and llm_safe_short(f) and self._llm_allowed(f)]
        long_ = [f for f in pending if f not in short]
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
        long_ += owner_only
        assumptions: list[Assumption] = []
        if self.s.answers.autonomy != "strict":      # ADR-0052: assume a safe answer where one exists
            vol_fields = [f for f in mapped.skipped_voluntary if f.key in voluntary]
            assumptions, long_ = await self._assume(long_, vol_fields, ctx)
            answers += [x.answer for x in assumptions]
            voluntary = {k: v for k, v in voluntary.items() if k not in {x.field.key for x in assumptions}}
            if assumptions:
                audit["assumed"] = {x.field.key: {"rule": x.answer.source, "why": x.answer.rationale or ""}
                                    for x in assumptions}
        out = self._finalize(ref, ctx, gate, answers, long_, tasks, audit, hints=mapped.hints, voluntary=voluntary,
                             profile_gaps=mapped.profile_gaps, no_gap_keys=mapped.no_gap_keys)
        if assumptions:
            out.profile_gaps += self._track_assumed(ref, ctx, assumptions)
        return out

    # ------------------------------------------------------------- autonomy (ADR-0052)
    def _owner_answer(self, key: str, company: str) -> str | None:
        """A stored owner answer for an assumed question kind (scoped persona answers first)."""
        with contextlib.suppress(Exception):
            if self.persona is not None and (v := self.persona.stored(key)) is not None:
                return v
        with contextlib.suppress(Exception):
            if self.gaps is not None:
                return self.gaps.answer(key, company=company)
        return None

    async def _assume(self, fields: list[FormField], voluntary: list[FormField],
                      ctx: _Ctx) -> tuple[list[Assumption], list[FormField]]:
        """Replace unresolved fields with a SAFE assumed answer where one exists (safe_default), plus conservative
        model answers to ordinary capability questions (infer). Hard limits are never assumed."""
        level = self.s.answers.autonomy
        out: list[Assumption] = []
        left: list[FormField] = []
        infer: list[FormField] = []
        prefs = [p for p in [self.s.answers.referral_default, *self.s.apply.referral_preference] if p]

        def take(f: FormField, value: Any, rule: str, why: str, kind: str | None, question: str = "",
                 conf: float = 1.0) -> None:
            stored = self._owner_answer(kind, ctx.posting.company) if kind else None
            if stored is not None:              # the owner answered this kind: it is no longer assumed
                v = autonomy.stored_value(f, stored)
                if v is None:
                    left.append(f)
                else:
                    out.append(Assumption(f, Answer(key=f.key, value=v, source="persona:answer"), None))
                return
            out.append(Assumption(f, Answer(key=f.key, value=value, source=f"assumed:{rule}", assumed=True,
                                            rationale=why, confidence=conf), kind, question))

        for f in [*fields, *voluntary]:
            if f.kind == FieldKind.file or geo_kind(f):
                left.append(f)
                continue
            chans = autonomy.contact_channels(f, ctx.posting.company)
            if chans is not None:
                if autonomy.hard_limit(f, contact=True) is None and (v := autonomy.decline_contact(f)) is not None:
                    take(f, v, "decline_contact_permission", f"contact permission ({', '.join(chans)}): declined",
                         autonomy.contact_kind(chans), autonomy.contact_question(chans, f.label))
                else:
                    left.append(f)
                continue
            if autonomy.hard_limit(f) is not None:
                left.append(f)
                continue
            if autonomy.eeo_field(f):
                v = autonomy.eeo_decline(f)
                if v is None:
                    left.append(f)               # no decline option: blank (optional) or the owner (required)
                else:
                    take(f, v, "eeo_decline", "decline to self-identify", None)
                continue
            if canonical_key(f.label) == "referral_source":
                v = autonomy.referral_default(f, prefs)
                if v is None:
                    left.append(f)
                else:
                    take(f, v, "referral_default", "configured 'how did you hear' default", "assumed.referral_source",
                         "What should 'How did you hear about us?' say when none of your configured sources is "
                         "offered? (the option or text to use)")
                continue
            if level == "infer" and f.required and autonomy.capability_question(f):
                infer.append(f)
                continue
            left.append(f)
        if infer:
            got, rest = await self._infer(infer, ctx)
            for f in infer:
                if f.key in got:
                    v, conf, why = got[f.key]
                    take(f, v, "infer", why, autonomy.infer_kind(f), f"{f.label[:160]} (Yes/No, or the option to pick)",
                         conf)
            left += rest
        keep = {f.key for f in left}
        return out, [f for f in fields if f.key in keep]

    async def _infer(self, fields: list[FormField], ctx: _Ctx) -> tuple[dict[str, tuple[Any, float, str]],
                                                                        list[FormField]]:
        """ONE conservative model call; an affirmative answer needs confidence >= answers.infer_min_confidence AND
        every topic word in the resume/profile facts, otherwise the honest No / lowest option."""
        if self.router is None:
            return {}, list(fields)
        facts = list(self.profile.facts(" ".join(f.label for f in fields), 20))
        prompt = autonomy.infer_prompt(fields, facts, f"{ctx.posting.title} @ {ctx.posting.company}")
        try:
            raw = await self.router.complete(prompt, task_class="short", max_tokens=80 + 80 * len(fields),
                                             json_schema={"type": "object"})
            m = re.search(r"\{.*\}", raw, re.DOTALL)
            data = json.loads(m.group(0)) if m else {}
        except Exception:  # noqa: BLE001 - a flaky model must never break planning
            return {}, list(fields)
        got: dict[str, tuple[Any, float, str]] = {}
        rest: list[FormField] = []
        all_facts = facts + list(self.profile.facts("", 60))
        for f in fields:
            d = data.get(f.key) if isinstance(data, dict) else None
            low = autonomy.lowest_value(f)
            if not isinstance(d, dict):
                rest.append(f)
                continue
            try:
                conf = max(0.0, min(1.0, float(d.get("confidence") or 0)))
            except (TypeError, ValueError):
                conf = 0.0
            why = re.sub(r"\s+", " ", str(d.get("rationale") or ""))[:160]
            v = self._validate(f, d.get("value")) if d.get("value") not in (None, "") else None
            if v is not None and v != low and not (conf >= self.s.answers.infer_min_confidence
                                                   and autonomy.topic_supported(f.label, all_facts)):
                v, why = low, f"not clearly supported by the resume (model said {d.get('value')!r}); lowest option"
            if v is None:
                v = low
                why = why or "no usable model answer; lowest option"
            if v is None:
                rest.append(f)
                continue
            got[f.key] = (v, conf, why or "model answer")
        return got, rest

    def _track_assumed(self, ref: str, ctx: _Ctx, assumptions: list[Assumption]) -> list[dict[str, Any]]:
        """Evidence + pattern tracking: count each assumed kind; at answers.promote_after the question becomes a
        profile gap the owner is asked once. Never breaks planning."""
        rows = [{"key": x.field.key, "label": x.field.label[:160], "value": x.answer.value, "rule": x.answer.source,
                 "assumed": x.answer.assumed, "confidence": x.answer.confidence, "rationale": x.answer.rationale or "",
                 **({"kind": x.kind} if x.kind else {})} for x in assumptions]
        if any(r["assumed"] for r in rows):
            autonomy.write_evidence(self.s.paths.data_dir, ref, rows)
        promoted: list[dict[str, Any]] = []
        if self.gaps is None:
            return promoted
        n_max = self.s.answers.promote_after
        for x in assumptions:
            if not x.kind or not x.answer.assumed:
                continue
            try:
                n = self.gaps.record_assumed(x.kind, ref, x.answer.source)
                if n >= n_max:
                    self.gaps.record_hit(x.kind, x.question or x.field.label, ref=ref, kind="default",
                                         default_used=f"assumed: {x.answer.value}", company=ctx.posting.company,
                                         title=ctx.posting.title)
                    promoted.append({"key": x.field.key, "gap": x.kind, "assumed_times": n,
                                     "note": "assumed repeatedly; ask the owner once (profile_questions)"})
            except Exception:  # noqa: BLE001, S112
                continue
        return promoted

    def _finalize(self, ref: str, ctx: _Ctx, gate: GateResult, answers: list[Answer], long_: list[FormField],
                  tasks: list[dict[str, Any]], audit: dict[str, Any], *, hints: dict[str, dict[str, Any]] | None = None,
                  voluntary: dict[str, str] | None = None,
                  profile_gaps: list[dict[str, Any]] | None = None, no_gap_keys: set[str] | None = None) -> PlanOut:
        """Cache the plan, issue the server-held approval, and list unresolved fields as client_tasks.
        Only `answers` are ever approved/submitted; drafts inside client_tasks never are (ADR-0041)."""
        hints = hints or {}
        unresolved_keys: list[str] = []
        have = {t["key"] for t in tasks}
        for f in long_:
            if f.key in have:
                continue
            if not f.required and not (f.kind == FieldKind.textarea and self.s.apply.write_optional):
                continue
            unresolved_keys.append(f.key)
            tasks.append(self._task(f, ctx, {"known": hints[f.key]} if f.key in hints else None))
        unresolved_keys = list(dict.fromkeys(unresolved_keys + [t["key"] for t in tasks]))
        # ADR-0040 x ADR-0047: a per-country authorization gap is recorded with its country
        # ("authorization.CA.requires_sponsorship"), never as the unscoped home-country key
        country_gaps = [g for g in (profile_gaps or []) if g.get("key") in unresolved_keys]
        scoped = {g["key"] for g in country_gaps} | set(no_gap_keys or ())   # unconfirmed country: no key at all
        self._record_gaps(ref, ctx, answers, [k for k in unresolved_keys if k not in scoped], country_gaps)
        planned = {a.key: a.value for a in answers}
        self._plans[ref] = _Cached(planned, unresolved_keys, ctx.posting, ctx.form,
                                   time.monotonic() + self.s.apply.plan_ttl_s, ctx.adapter, ctx.summary,
                                   gate, list(answers), audit,
                                   {"hints": hints, "voluntary": voluntary, "profile_gaps": profile_gaps,
                                    "no_gap_keys": no_gap_keys})
        _, files = self._split(ctx.form, planned)
        aid = self.approvals.issue(ref, self._url(ctx), ctx.posting.company, ctx.posting.title, planned, files)
        self.approvals.set_state(ref, "planned", company=ctx.posting.company, title=ctx.posting.title,
                                 approval_id=aid, url=self._url(ctx), only_from=("planned", "approved", "failed", "handoff"))
        return PlanOut(plan=ApplicationPlan(ref=ref, gate=gate, answers=answers, unresolved=unresolved_keys,
                                                audit=audit),
                       client_tasks=tasks, approval_id=aid, timings=self.timings.get(ref, {}),
                       skipped_voluntary=voluntary or {}, profile_gaps=list(profile_gaps or []))

    def _salary(self, posting: Posting):
        """Persona salary policy when wired (ADR-0047); otherwise the deterministic rule from PolicyCfg."""
        if self.persona is None:
            return compute_ask(self.s.policy, posting)
        exp = getattr(getattr(getattr(self.profile, "p", None), "salary", None), "expected", None)
        return self.persona.salary(self.s.policy, posting, exp)

    def _record_gaps(self, ref: str, ctx: _Ctx, answers: list[Answer], unresolved: list[str],
                     country_gaps: list[dict[str, Any]] | None = None) -> None:
        """Record unresolved fields and default-used answers as profile gaps; must never break planning."""
        if self.gaps is None:
            return
        try:
            from ..persona.collector import collect_gaps
            collect_gaps(self.gaps, self.persona, self.profile, ref=ref, posting=ctx.posting,
                         fields=ctx.form.fields, answers=answers, unresolved=unresolved)
            labels = {f.key: (f.label, f.required) for f in ctx.form.fields}
            for g in country_gaps or []:
                label, required = labels.get(g["key"], ("", False))
                self.gaps.record_hit(f"authorization.{g['country']}.{g['fact']}", label, ref=ref, kind="unresolved",
                                     blocked=bool(required), company=ctx.posting.company, title=ctx.posting.title)
        except Exception:  # noqa: BLE001, S110
            pass

    def _company_aliases(self, company: str) -> list[str]:
        """dedupe.company_aliases names that are the same employer as the posting company (ADR-0038)."""
        from .adapters import same_company
        out: list[str] = []
        for brand, canon in self.s.dedupe.company_aliases.items():
            if same_company(brand, company) or same_company(canon, company):
                out += [x for x in (brand, canon) if not same_company(x, company)]
        return out

    def _skip_cover(self, f: FormField) -> bool:
        """Cover letters only when the form requires one (writing.cover_letter_policy, ADR-0023)."""
        return ("cover" in normalize(f.label) and not f.required
                and self.s.writing.cover_letter_policy != "always")

    def _is_prose_question(self, f: FormField) -> bool:
        """A question-shaped label (why/what/describe...) in a free-text box with no options and room for prose.
        Names, links, numbers and anything the mapper recognises are never prose (ADR-0033)."""
        if f.kind == FieldKind.textarea:
            return not f.options
        if f.kind not in (FieldKind.text, FieldKind.unknown) or f.options or canonical_key(f.label) is not None:
            return False
        if f.max_length and f.max_length <= self.s.apply.short_text_max:
            return False
        return len(f.label.split()) >= 4 and bool(_PROSE_Q.search(f.label)) and not _NOT_PROSE_Q.search(f.label)

    @staticmethod
    def _needs_human(f: FormField) -> bool:
        if _ATTEST_LABEL.search(f.label):
            return True
        return bool(_AI_LABEL.search(f.label) and not (not f.options and _AI_TOOLS_PROSE.search(f.label)))

    def _is_short(self, f: FormField) -> bool:
        if f.kind == FieldKind.file:
            return False
        if f.kind != FieldKind.textarea and self._is_prose_question(f):
            return False
        if f.kind == FieldKind.textarea:
            return bool(f.max_length and f.max_length <= self.s.apply.short_text_max)
        return True

    def _llm_allowed(self, f: FormField) -> bool:
        return not (canonical_key(f.label) in _SENSITIVE_KEYS or _SENSITIVE_LABEL.search(f.label) or eeo_class(f.label)[0]
                    or never_llm(f)
                    or self._needs_human(f) or _NO_FABRICATE_LABEL.search(f.label)
                    or geo_kind(f) or f.kind == FieldKind.file)

    def _task(self, f: FormField, ctx: _Ctx, extra: dict[str, Any] | None = None) -> dict[str, Any]:
        t: dict[str, Any] = {"key": f.key, "label": f.label, "max_len": f.max_length or 0,
                             "context": {"role": f"{ctx.posting.title} @ {ctx.posting.company}",
                                         "posting": ctx.summary, "facts": self.profile.facts(f.label, 6)}}
        if f.options:
            t["options"] = f.options[:30]
        if self._needs_human(f):
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

    async def _write_long(self, fields: list[FormField], ctx: _Ctx, *, deferred: bool | None = None):
        """Prose only for required (or opted-in) long text; fields written concurrently under ONE wall-clock
        budget (writing.timeout_s) shared by writer and reviewer rounds. In deferred mode nothing is drafted:
        prose fields are listed for draft_answers (ADR-0041)."""
        written: list[Answer] = []
        rest: list[FormField] = []
        tasks: list[dict[str, Any]] = []
        w = self.s.writing
        if not fields or self.router is None or not w.enabled:
            return written, list(fields), tasks
        todo: list[FormField] = []
        for f in fields:
            wanted = f.required or self.s.apply.write_optional or "cover" in normalize(f.label)
            if (self._is_prose_question(f) and wanted and self._llm_allowed(f)
                    and not ("cover" in normalize(f.label) and w.cover_letter_policy == "never")):
                todo.append(f)
            else:
                rest.append(f)
        if deferred if deferred is not None else _write_mode(self.s) == "deferred":
            tasks += [self._task(f, ctx, {"deferred": True}) for f in todo]   # no model call at all
            return written, rest, tasks
        if self._exemplar is None:
            self._exemplar = load_exemplar(w.style_exemplar)
        avail = getattr(self.router, "available", None)
        if todo and avail is not None:
            try:
                if not await avail("narrative"):
                    return written, rest + todo, tasks
            except Exception:  # noqa: BLE001
                return written, rest + todo, tasks

        loop = asyncio.get_running_loop()
        deadline = loop.time() + w.timeout_s if w.timeout_s and w.timeout_s > 0 else None
        progress = {f.key: WriteProgress() for f in todo}

        async def one(f: FormField):
            cover = "cover" in normalize(f.label)
            coro = write_text(
                self.router, w, kind="cover_letter" if cover else "free_text", label=f.label,  # type: ignore[arg-type]
                company=ctx.posting.company, title=ctx.posting.title, summary=ctx.summary,
                facts=self.profile.facts(f.label, 12), max_len=f.max_length, exemplar=self._exemplar or "",
                resume=getattr(self.profile, "resume_facts", None), corpus=getattr(self.profile, "corpus", None),
                progress=progress[f.key])
            if deadline is None:
                return await coro
            async with asyncio.timeout_at(deadline):   # cancels the in-flight LLM call (and its HTTP request)
                return await coro

        with self._stage(ctx.form.ref, "write"):
            results = await bounded_gather((one(f) for f in todo), self.s.concurrency.global_limit)
        for f, res in zip(todo, results, strict=True):
            if isinstance(res, TimeoutError):
                p = progress[f.key]
                extra: dict[str, Any] = {"timed_out": True, "stage": p.stage or "queued"}
                if p.draft:   # partial draft for the client to finish; never planned as an answer
                    extra |= {"draft": p.draft, "review": review_rubric(
                        w, company=ctx.posting.company, title=ctx.posting.title, facts=self.profile.facts(f.label, 12))}
                tasks.append(self._task(f, ctx, extra))
            elif isinstance(res, BaseException):
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

    async def draft(self, ref: str) -> PlanOut:
        """draft_answers: run writer + reviewer for the prose fields a (deferred) plan left open, under the same
        writing.timeout_s budget, and re-issue the approval over the grown answer set (ADR-0041)."""
        cached = self._cached(ref)
        if cached is None or cached.adapter is None or cached.gate is None:
            out = await self.plan(ref)
            if out.plan.gate.decision != Decision.submit:
                return out
            cached = self._cached(ref)
            if cached is None or cached.gate is None:
                return out
        ctx = _Ctx(cached.adapter, cached.posting, cached.form, cached.summary)
        open_ = [f for f in ctx.form.fields if f.key in cached.unresolved]
        from ..persona import posting_scope
        with self._llm_timings(ref), self._stage(ref, "llm"), posting_scope(
                ctx.posting.company, ctx.posting.title, ctx.summary):
            written, rest, tasks = await self._write_long(open_, ctx, deferred=False)
        out = self._finalize(ref, ctx, cached.gate, cached.plan_answers + written, rest, tasks, cached.audit,
                             **cached.extra)
        out.timings = dict(self.timings.get(ref, {}))
        return out

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
            from .. import consent  # one human consent per window (ADR-0048)
            c = consent.use(self.s, "approve_submissions", f"ref={ref} approval={rec.approval_id[:8]}")
            if c is not None:
                self.approvals.approve(rec.approval_id, f"consent:{c.id}")
                return rec.approval_id, "", f"consent:{c.id}"
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
        if release is None or not event_id or self._submission_state(ref) not in _NOT_SENT:   # no id: fail closed
            return
        with contextlib.suppress(Exception):   # a failed release only keeps the count higher (fail closed)
            await release(ref, reason, event_id)

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

    def _client_prose_review_on(self) -> bool:
        """Agent mode (and auto-approve, which is agent-only): nobody but the client sees its prose before an
        approval covers it, so it gets the writer path's review. Human mode: a human approves (ADR-0044)."""
        return self.s.apply.approval_mode == "agent" and self.s.writing.review_client_prose

    def _digest(self, ref: str, key: str, text: str) -> str:
        return hashlib.sha256(f"{ref}\0{key}\0{text}".encode()).hexdigest()

    async def _review_client_prose(self, ctx: _Ctx, eff: dict[str, Any],
                                   base: dict[str, Any]) -> list[dict[str, Any]]:
        """Review free-text answers the client supplied (not identical to a server-written/approved answer) under
        writing.timeout_s. Returns needs_review client_tasks for every field that did not pass; [] when all pass.
        Fails closed: a timeout or reviewer error is a failure (ADR-0044)."""
        if not self._client_prose_review_on():
            return []
        ref = ctx.form.ref
        fields = {f.key: f for f in ctx.form.fields}
        failed: list[dict[str, Any]] = []
        todo: list[tuple[FormField, str]] = []
        for k, v in eff.items():
            if base.get(k) == v and k in base:
                continue                                         # server-written or already approved
            f = fields.get(k)
            if f is None:                                        # not a form field: never carried to the adapter
                if v not in (None, ""):
                    failed.append({"key": k, "needs_review": True, "violations": ["[field] not a field of this form"]})
                continue
            if v is None or v is False or v == "":
                continue                                         # nothing filled / declined
            if not (isinstance(v, str | bool | int | float)
                    or (isinstance(v, list | tuple) and all(isinstance(x, str) for x in v))):
                failed.append(self._task(f, ctx, {"needs_review": True, "violations": [
                    f"[type] answer must be a string, bool, number or list of strings, got {type(v).__name__}"]}))
                continue
            if self._needs_human(f) or _CONSENT_LABEL.search(f.label) or contact_permission_channels(
                    f.label, choice=f.kind in (FieldKind.boolean, FieldKind.select, FieldKind.multiselect) or bool(f.options)):
                failed.append(self._task(f, ctx, {"needs_review": True, "violations": [
                    "[attestation] attestation/consent field: only a profile answer or a human may set it"]}))
                continue
            texts = [v] if isinstance(v, str) else [x for x in v if isinstance(x, str)] \
                if isinstance(v, list | tuple) else []
            bad = next((b for b in (self._text_guard(x, x in f.options) for x in texts) if b), None)
            if bad:                                              # never reaches the reviewer or the form
                failed.append(self._task(f, ctx, {"needs_review": True, "draft": str(v)[:2000], "violations": [bad]}))
                continue
            if f.kind not in _FREE_TEXT_KINDS or self._deterministic(f, v):
                continue
            if not isinstance(v, str):                           # lists, dicts, ...: never coerced into free text
                failed.append(self._task(f, ctx, {"needs_review": True, "violations": [
                    f"[type] free-text answer must be a string, got {type(v).__name__}"]}))
                continue
            if not v.strip():
                continue
            if self._digest(ref, k, v) not in self._reviewed:
                todo.append((f, v))
        if not todo:
            self._audit_rejected(ref, failed)
            return failed
        w = self.s.writing
        if self._exemplar is None:
            self._exemplar = load_exemplar(w.style_exemplar)
        loop = asyncio.get_running_loop()
        deadline = loop.time() + w.timeout_s if w.timeout_s and w.timeout_s > 0 else None
        resume = getattr(self.profile, "resume_facts", None)
        corpus = getattr(self.profile, "corpus", None)

        async def one(f: FormField, text: str):
            coro = review_text(
                self.router, w, text, kind="cover_letter" if "cover" in normalize(f.label) else "free_text",
                label=f.label, company=ctx.posting.company, title=ctx.posting.title, summary=ctx.summary,
                facts=self.profile.facts(f.label, 12), max_len=f.max_length, exemplar=self._exemplar or "",
                resume=resume, corpus=corpus)
            if deadline is None:
                return await coro
            async with asyncio.timeout_at(deadline):
                return await coro

        with self._llm_timings(ref), self._stage(ref, "review_client"):
            results = await bounded_gather((one(f, v) for f, v in todo), self.s.concurrency.global_limit)
        for (f, text), res in zip(todo, results, strict=True):
            if isinstance(res, TimeoutError):
                why, card, rubric = [f"[timeout] review exceeded writing.timeout_s={w.timeout_s:g}s (fail closed)"], {}, ""
            elif isinstance(res, BaseException):
                why, card, rubric = [f"[error] reviewer failed: {type(res).__name__} (fail closed)"], {}, ""
            elif res.status == "pass":
                self._reviewed.add(self._digest(ref, f.key, text))
                continue
            else:
                why, card, rubric = [v.line() for v in res.violations][:6], res.scorecard, res.rubric
            extra: dict[str, Any] = {"needs_review": True, "draft": text, "violations": why}
            if rubric:
                extra["review"] = rubric
            if card:
                extra["scorecard"] = card
            failed.append(self._task(f, ctx, extra))
        self._audit_rejected(ref, failed)
        return failed

    def _audit_rejected(self, ref: str, failed: list[dict[str, Any]]) -> None:
        if failed:
            self.approvals.audit("client_prose_rejected", ref=ref, actor="review",
                                 detail=",".join(t["key"] for t in failed)[:200])

    def _deterministic(self, f: FormField, v: Any) -> bool:
        """Exact values that need no prose review: the profile's own value for a recognised key, a URL, a number,
        a date, or an option of the field's fixed set (ADR-0044)."""
        if v is None:
            return True                                          # nothing is filled
        if isinstance(v, bool):
            return bool(f.options) and str(v) in f.options
        if isinstance(v, int | float):
            return True
        if not isinstance(v, str):
            return False
        t = v.strip()
        if f.options and t in f.options:
            return True
        if self._is_prose_question(f):
            return False                                         # a URL or number in a "Why..." box is still prose
        key = canonical_key(f.label)
        if key is not None:
            with contextlib.suppress(Exception):
                pv = self.profile.lookup(key)
                if pv is not None and str(pv).strip() == t:
                    return True
        return bool(_EXACT_VALUE.fullmatch(t))

    def _text_guard(self, text: str, option: bool = False) -> str | None:
        """Deterministic checks on every client string before any reviewer call (ADR-0044). Deny by default: any
        code point outside the prose allowlist is `[charset]` (skipped only for an exact option of the field's
        fixed set, which is form text, not client text). Then stop phrases, attestation/no-AI/consent wording and
        reviewer-addressed text are scanned on the normalised views from `checks.scan_forms`."""
        bad = checks.charset_violations(text)
        if bad and not option:
            return f"[charset] characters outside the allowlist {bad[:4]}"
        forms = checks.scan_forms(text)
        for t in forms:
            low = t.lower()
            for p in self.s.policy.stop_phrases:
                if p.strip() and re.search(rf"(?<!\w){re.escape(p.lower())}(?!\w)", low):
                    return f"[attestation] attestation/consent text in an answer: {p!r}"
            m = _ATTEST_TEXT.search(t)
            if m:
                return f"[attestation] attestation/consent text in an answer: {m.group(0)!r}"
        m = _INJECT_TEXT.search(forms[0])
        if m:
            return f"[injection] text addressed to the reviewer: {m.group(0)!r}"
        return None

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
                base = cached.answers
            elif dry or self.s.apply.auto_approve_active:
                plan = await self._plan_ctx(ref, ctx)
                if plan.plan.gate.decision != Decision.submit:
                    return SubmitResult(ref=ref, status="duplicate" if "duplicate" in plan.plan.gate.reasons
                                        else "needs_human", detail="; ".join(plan.plan.gate.reasons)[:200])
                base = {a.key: a.value for a in plan.plan.answers}
                eff = {**base, **supplied}
            else:
                eff = supplied                      # stateless: must hash-match the server-held record
                rec, _ = self.approvals.check(approval_id, ref, self._url(ctx)) if approval_id else (None, "")
                base = eff if rec is not None and self.approvals.matches(rec, eff, self._split(ctx.form, eff)[1]) else {}
            failed = await self._review_client_prose(ctx, eff, base)
            if failed:
                return SubmitResult(ref=ref, status="needs_review", client_tasks=failed,
                                    detail=f"client prose failed review: {[t['key'] for t in failed][:8]}")
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
            async with self._lock(ctx.posting.company), self._otp_guard(ref, ctx.posting.platform, dry):
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
                clicked_at = datetime.now(UTC)
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
                if not dry and result.status == "awaiting_code" and result.pending_id:
                    result = await self._unattended_code(result, ctx.posting.platform, clicked_at)
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
        return await self._enter_code(drv, pending_id, ref, code, "user", "verification code entered by the user")

    async def _enter_code(self, drv: Any, pending_id: str, ref: str, code: str, actor: str,
                          why: str) -> SubmitResult:
        if not self.approvals.set_state(ref, "submitting", detail=why, only_from=("awaiting_code",)):
            return SubmitResult(ref=ref, status="error", detail="submission is not awaiting a code")
        self.approvals.audit("code_provided", ref=ref, actor=actor, detail="code entered (value not recorded)")
        try:
            res = await drv.provide_code(pending_id, code)
        except BaseException:
            self.approvals.finish(ref, "unknown", "code entry interrupted; verify manually")
            raise
        if res is None:
            res = SubmitResult(ref=ref, status="error", detail="unknown or expired pending_id")
        return await self._apply_code_result(res)

    def _sender_domain(self, ref: str, platform: str | None) -> str | None:
        doms = self.s.verification.sender_domains
        return doms.get(ref.split(":")[0]) or doms.get(str(platform or "").lower())

    def _otp_guard(self, ref: str, platform: str | None, dry: bool):
        """Inside an otp_unattended window, one submission per mail sender domain is between its click and its code
        at a time, so the newest message from that sender can only belong to this application (ADR-0048)."""
        if dry:
            return contextlib.nullcontext()
        domain = self._sender_domain(ref, platform)
        if not domain:
            return contextlib.nullcontext()
        from .. import consent
        if consent.active(self.s, "otp_unattended") is None:
            return contextlib.nullcontext()
        return self._otp_locks.setdefault(domain.lower(), asyncio.Lock())

    async def _unattended_code(self, res: SubmitResult, platform: str, clicked_at: datetime) -> SubmitResult:
        """otp_unattended (ADR-0048). Outside a consent window: unchanged (the human / otp_assist path). Inside: read
        the newest code mail from the expected sender, enter it once, continue confirmation detection; no usable
        mail -> `unknown`, never retried. The code is never logged, stored, audited or returned."""
        from .. import consent
        from ..consent.otp import fetch_code

        pid, ref = res.pending_id or "", res.ref
        c = consent.use(self.s, "otp_unattended", f"ref={ref}")
        if c is None:
            return res
        if not any(i.strip() for i in self.s.verification.trusted_authserv_ids):   # fail closed: human path
            self.approvals.audit("code_unattended_off", ref=ref, actor=f"consent:{c.id}",
                                 detail="verification.trusted_authserv_ids is empty")
            return res.model_copy(update={"detail": f"{res.detail or ''}; otp: needs_human ({OTP_SETUP_HINT})"[:500]})
        drv = next((d for d in self._code_drivers() if d.pending_info(pid)), None)
        if drv is None:
            return res
        actor = f"consent:{c.id}"

        async def give_up(why: str) -> SubmitResult:
            self.approvals.audit("code_unattended_failed", ref=ref, actor=actor, detail=why[:120])
            r = await drv.abandon_pending(pid, f"unattended code: {why}")
            if r is None:
                return res
            out = await self._apply_code_result(r)                   # handoff -> state `unknown` (blocking)
            return out.model_copy(update={"status": "roadblock", "pending_id": None,
                                          "detail": f"unknown (never retried): {out.detail or ''}"[:400]})

        domain = self._sender_domain(ref, platform)
        if not getattr(self.s, "_otp_mail_trusted", False):
            return await give_up("mail settings are not from the trusted user config (sender domains / mail source)")
        if not domain:
            return await give_up("no expected sender domain for this platform (verification.sender_domains)")
        try:
            got = await fetch_code(self.s, sender_domain=domain, clicked_at=clicked_at)
        except Exception as e:  # noqa: BLE001 - message dropped: it could quote mail content
            return await give_up(f"mail read failed ({type(e).__name__})")
        if got.code is None:
            return await give_up(got.detail or got.status)
        if drv.pending_check(pid, got.code.reveal()):                # format mismatch: never typed
            return await give_up("the mailed code does not fit the page's code format")
        try:
            return await self._enter_code(drv, pid, ref, got.code.reveal(), actor,
                                          f"verification code entered unattended ({actor})")
        except Exception as e:  # noqa: BLE001 - state already `unknown`; no exception text (no code) escapes
            return SubmitResult(ref=ref, status="roadblock",
                                detail=f"unknown (never retried): code entry failed ({type(e).__name__})")
        finally:
            got.code = None

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
            if g := self.profile.lookup("gender_if_required"):   # the driver fills required EEO selects only
                demo["demographic:gender"] = g
            rows = getattr(getattr(self.profile, "p", None), "education_rows", list)()
            edu = {f"education:{i}": r for i, r in enumerate(rows[:1])}
            # DOM-only context for the driver (ADR-0039): phone-widget country, opt-in privacy-notice acknowledgement.
            dom = {"contact:country": self.profile.lookup("country") or "",
                   "pref:acknowledge_privacy_notices": self.profile.lookup("acknowledge_privacy_notices") is True}
            async with self._browser_sem:
                ctx.meta["dispatched"] = True
                last = await drv.fill_and_submit(url, ctx.form, {**values, **demo, **edu, **dom}, files, dry_run=dry)
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
                        "client_tasks": out.client_tasks, "approval_id": out.approval_id, "t": out.timings,
                        **({"skipped_voluntary": out.skipped_voluntary} if out.skipped_voluntary else {})}
            if not dry and not self.s.apply.auto_approve_active:
                return {"ref": ref, "status": "awaiting_approval", "approval_id": out.approval_id, "t": out.timings}
            async with browser_cap:
                r = await self.submit(ref, {}, out.approval_id, dry)
            row = {"ref": ref, "status": r.status, "via": r.via, "detail": r.detail, "pending_id": r.pending_id,
                   "t": self.timings.get(ref, {}), "skipped_voluntary": out.skipped_voluntary}
            return {k: v for k, v in row.items() if v}

        # every ref starts at once; the pools above (plan_sem, browser_cap/_browser_sem) do the limiting
        rows = await bounded_gather((one(r) for r in refs), max(len(refs), 1))
        return [r if not isinstance(r, BaseException) else {"status": "error", "detail": str(r)[:160]}
                for r in rows]

"""Pure, configurable policy gates (ADR-0009).

A gate is a small function `(GateContext, PolicyCfg) -> Verdict | None` (None = pass). Gates are
registered by name in `GATES`; `PolicyCfg.gates` enables/reorders them. `evaluate()` combines verdicts:
any skip wins, else any needs_human, else submit. Reasons are short strings.

No I/O, no platform imports. Text flags may be passed in (e.g. `Posting.flags` from the shared
text-flagging helper); if absent a tiny local fallback derives them.
"""
from __future__ import annotations

import re
from collections.abc import Callable
from dataclasses import dataclass, field

from ..config import PolicyCfg
from ..core.models import Decision, GateResult, Posting

Verdict = tuple[Decision, str]
Gate = Callable[["GateContext", PolicyCfg], "Verdict | None"]


@dataclass
class GateContext:
    company: str = ""
    title: str = ""
    location: str = ""
    salary: str = ""
    text: str = ""                  # posting description (and anything else worth scanning)
    form_text: str = ""             # application form labels / attestations, one field per line (ADR-0040)
    flags: dict[str, list[str]] = field(default_factory=dict)
    graduation: str = ""            # ADR-0059: the profile's graduation (YYYY-MM); wins over policy.graduation

    @classmethod
    def from_posting(cls, p: Posting, form_text: str = "", graduation: str = "") -> GateContext:
        return cls(p.company, p.title, p.location, p.salary or "", p.description or "", form_text,
                   dict(p.flags), graduation or "")


# -- local fallback flagger ---------------------------------------------------------------------
_FLAG_PATTERNS: dict[str, str] = {
    "no_sponsorship": r"(?:will|do|does|can)\s*(?:not|n't)\s+(?:provide\s+|offer\s+)?(?:visa\s+)?sponsor"
                      r"|unable to (?:provide\s+)?(?:visa\s+)?sponsor|no (?:visa )?sponsorship"
                      r"|without (?:the need for )?(?:visa )?sponsorship|not (?:eligible|available) for sponsorship"
                      r"|sponsorship (?:is )?not (?:available|offered|provided)"
                      r"|must be (?:legally )?authorized to work[^.]{0,60}without",
    # ADR-0059: an explicit requirement sentence only (not EEO "citizenship status", not "citizen or eligible ...")
    "citizenship": r"(?:u\.?s\.?|united states) citizen(?:ship)? (?:is )?(?:required|mandatory)"
                   r"|must (?:be|hold) (?:a )?(?:u\.?s\.?|united states) citizen(?:ship)?(?!\w*,? *(?:or|and/or)\b)"
                   r"|(?:u\.?s\.?|united states) citizens? only|only (?:open to )?(?:u\.?s\.?|united states) citizens",
    "clearance": r"security clearance|\bts/sci\b|top secret|secret clearance|active clearance|polygraph",
    "us_person": r"\bitar\b|u\.?s\.?[ -]persons?\b|\bear\b regulations"
                 r"|(?:u\.?s\.?|united states) citizens? or (?:lawful )?permanent residents?",
}


def local_flags(text: str) -> dict[str, list[str]]:
    """Minimal fallback for the shared flagging helper. Returns {flag: [matched snippets]}."""
    out: dict[str, list[str]] = {}
    for k, pat in _FLAG_PATTERNS.items():
        hits = [m.group(0).strip() for m in re.finditer(pat, text, re.IGNORECASE)]
        if hits:
            out[k] = hits[:3]
    return out


def _legal_kind(snippet: str) -> str:
    """ADR-0059: the posting flag "citizenship" also carries clearance and ITAR snippets; sort each to its gate."""
    if re.search(_FLAG_PATTERNS["clearance"], snippet, re.IGNORECASE):
        return "clearance"
    if re.search(_FLAG_PATTERNS["citizenship"], snippet, re.IGNORECASE):
        return "citizenship"
    if re.search(_FLAG_PATTERNS["us_person"], snippet, re.IGNORECASE):
        return "us_person"
    return ""                       # a mention that states no requirement (EEO / export boilerplate)


def _flags(ctx: GateContext) -> dict[str, list[str]]:
    merged = dict(local_flags(ctx.text))
    for k, v in (ctx.flags or {}).items():
        if k == "citizenship":
            for snip in v:
                if kind := _legal_kind(snip):
                    merged.setdefault(kind, []).append(snip)
            continue
        merged.setdefault(k, list(v))
    return merged


# -- gates --------------------------------------------------------------------------------------
_YEARS = re.compile(
    r"(\d{1,2})\s*(?:\+|plus)?\s*(?:(?:-|–|to)\s*(\d{1,2})\s*)?\+?\s*(?:years?|yrs?)\b"
    r"[^.\n]{0,60}?(?:experience|exp\b|in\b|working|developing|building)", re.IGNORECASE)
_YEARS_ALT = re.compile(r"(?:minimum|at least|min\.?)\s*(?:of\s*)?(\d{1,2})\s*\+?\s*(?:years?|yrs?)", re.IGNORECASE)


def required_years(text: str) -> int | None:
    """Largest *minimum* years-of-experience requirement found in the text."""
    vals = [int(m.group(1)) for m in _YEARS.finditer(text)]
    vals += [int(m.group(1)) for m in _YEARS_ALT.finditer(text)]
    return max(vals) if vals else None


def years_gate(ctx: GateContext, cfg: PolicyCfg) -> Verdict | None:
    if cfg.max_years_required is None:
        return None
    n = required_years(ctx.text + " " + ctx.title)
    if n is not None and n > cfg.max_years_required:
        return Decision.skip, f"needs {n}+ yrs (max {cfg.max_years_required})"
    return None


def _ym(s: str) -> tuple[int, int] | None:
    m = re.fullmatch(r"\s*(\d{4})-(\d{1,2})\s*", s or "")
    return (int(m.group(1)), int(m.group(2))) if m else None


def graduation_gate(ctx: GateContext, cfg: PolicyCfg) -> Verdict | None:
    """ADR-0059: skip only on a clear mismatch between the posting's stated graduation windows and the profile's
    graduation (policy.graduation when the profile has none), widened by policy.graduation_flex_months. Ambiguous or
    open-ended wording ("by 2028", "2026, 2027 or 2028", "2028 or earlier") never skips."""
    from ..core.gradwindow import grad_mismatch
    grad = ctx.graduation or cfg.graduation or ""
    if not _ym(grad):
        return None
    if grad_mismatch(ctx.text + "\n" + ctx.title, grad, cfg.graduation_flex_months):
        return Decision.skip, "grad date outside posting window"
    return None


def sponsorship_gate(ctx: GateContext, cfg: PolicyCfg) -> Verdict | None:
    if cfg.requires_sponsorship and "no_sponsorship" in _flags(ctx):
        return Decision.skip, "no sponsorship offered"
    return None


_CLEARANCE_SOFT = re.compile(r"(?:ability|able|eligib\w+|eligible|willing)\s+to\s+(?:obtain|get|acquire|hold)"
                             r"|obtain\w*\s+(?:a\s+)?(?:security\s+)?clearance", re.IGNORECASE)


def citizenship_gate(ctx: GateContext, cfg: PolicyCfg) -> Verdict | None:
    f = _flags(ctx)
    if "citizenship" in f and not cfg.us_citizen:
        return Decision.skip, "US citizenship required"
    if "us_person" in f and not (cfg.us_person or cfg.us_citizen):
        return Decision.skip, "US-person/ITAR restriction"
    if "clearance" in f and not cfg.has_clearance:
        soft = bool(_CLEARANCE_SOFT.search(ctx.text))
        if not (soft and cfg.us_citizen):
            return Decision.skip, "security clearance required"
    return None


_MONEY = re.compile(r"\$?\s*(\d[\d,]*(?:\.\d+)?)\s*([kK])?")


def parse_salary(s: str, hours_per_year: int = 2080) -> tuple[float, float] | None:
    """Parse '$120k - $150k', '$55/hr', '100,000-130,000 USD'. Returns annual (low, high) or None."""
    if not s:
        return None
    nums: list[float] = []
    for m in _MONEY.finditer(s):
        v = float(m.group(1).replace(",", ""))
        if m.group(2):
            v *= 1000
        nums.append(v)
    nums = [n for n in nums if n > 0]
    if not nums:
        return None
    lo, hi = min(nums[:2]), max(nums[:2])
    if re.search(r"/\s*h(?:ou)?r|per\s+hour|hourly|an hour", s, re.IGNORECASE) or hi < 500:
        lo, hi = lo * hours_per_year, hi * hours_per_year
    elif re.search(r"/\s*mo|per\s+month|monthly", s, re.IGNORECASE) and hi < 50000:
        lo, hi = lo * 12, hi * 12
    return lo, hi


def effective_floor(cfg: PolicyCfg, location: str) -> float | None:
    if cfg.salary_floor is None:
        return None
    loc = (location or "").lower()
    mult = 1.0
    for sub, m in cfg.col_multipliers.items():
        if sub.lower() in loc:
            mult = m
            break
    return cfg.salary_floor * mult


def salary_gate(ctx: GateContext, cfg: PolicyCfg) -> Verdict | None:
    floor = effective_floor(cfg, ctx.location)
    rng = parse_salary(ctx.salary, cfg.hours_per_year)
    if floor is None or rng is None:
        return None
    if rng[1] < floor:
        return Decision.skip, f"pay tops out ${rng[1]:,.0f} < floor ${floor:,.0f}"
    return None


def employer_gate(ctx: GateContext, cfg: PolicyCfg) -> Verdict | None:
    for pat in cfg.exclude_employer_patterns:
        try:
            hit = re.search(pat, ctx.company, re.IGNORECASE)
        except re.error:
            hit = pat.lower() in ctx.company.lower()
        if hit:
            return Decision.skip, f"excluded employer ({pat})"
    return None


_PHD_TITLE = re.compile(
    r"\bph\.?\s?d\.?\s*[-,]?\s*(?:graduates?|students?|candidates?|interns?|only|required|new grad\w*)"
    r"|\bnew\s+ph\.?\s?d\b|\brecent\s+ph\.?\s?d\b|\bph\.?\s?d\.?\s+(?:research|scientist|fellow)", re.IGNORECASE)


def title_gate(ctx: GateContext, cfg: PolicyCfg) -> Verdict | None:
    """Titles aimed at PhD graduates/students (unless cfg.has_phd) plus user `exclude_title_patterns`."""
    if not cfg.has_phd and _PHD_TITLE.search(ctx.title):
        return Decision.skip, "title targets PhD graduates/students"
    for pat in cfg.exclude_title_patterns:
        try:
            hit = re.search(pat, ctx.title, re.IGNORECASE)
        except re.error:
            hit = pat.lower() in ctx.title.lower()
        if hit:
            return Decision.skip, f"excluded title ({pat})"
    return None


# ADR-0040: a stop phrase matches whole words within one segment (one form field line, or one sentence of the posting
# text). Exactly two false-positive classes are removed, nothing else:
#   1. substrings of other words or spans across two fields/options ("no aim", "casino airline", "...No | AI tools");
#   2. in POSTING TEXT only, a "no ..." phrase followed by a requirement noun ("No AI experience", "no AI/ML
#      background"). Form labels and options never get this exemption.
# Required markers ("*", "(required)", "required", "needed", "mandatory") are stripped before matching and are never an
# exemption: "This is my own work Required" and "No AI - required" still stop.
_REQUIRED_MARKER = re.compile(r"\*|\(\s*(?:required|needed|mandatory)\s*\)|[\s,:-]*\b(?:required|needed|mandatory)\b\W*$",
                              re.IGNORECASE)
_REQUIREMENT_NOUN_AFTER = re.compile(r"^[ \t/,-]*(?:/?ml\b|/?machine learning\b)?[ \t/,-]*"
                                     r"(?:prior |previous |professional )?(?:experience|background|knowledge|expertise|"
                                     r"skills?|degree)\b", re.IGNORECASE)
# sentence ends: newline, ! ? ; or a period after a word of 2+ characters ("A.I." and "U.S." stay whole)
_SEGMENT_END = re.compile(r"\n|[!?;]|(?<=[A-Za-z0-9]{2})\.(?=\s|$)")


def _phrase_rx(phrase: str) -> re.Pattern[str] | None:
    words = re.findall(r"[a-z0-9]+", phrase.lower())
    if not words:
        return None
    return re.compile(r"(?<![a-z0-9])" + r"[ \t_/.-]+".join(map(re.escape, words)) + r"\.?(?![a-z0-9])", re.IGNORECASE)


def stop_phrase_hit(text: str, phrases: list[str], *, posting_text: bool = False) -> str | None:
    """The first stop phrase found as whole words in one segment of `text`, else None. `posting_text=True` (the job
    description only) lets "no <phrase> experience/background/..." through as a job requirement."""
    segments = [_REQUIRED_MARKER.sub(" ", x) for x in _SEGMENT_END.split(text or "") if x.strip()]
    for ph in phrases:
        rx = _phrase_rx(ph)
        if rx is None:
            continue
        exempt_ok = posting_text and re.findall(r"[a-z0-9]+", ph.lower())[:1] == ["no"]
        for seg in segments:
            for m in rx.finditer(seg):
                if not (exempt_ok and _REQUIREMENT_NOUN_AFTER.match(seg[m.end():])):
                    return ph
    return None


def stop_phrase_gate(ctx: GateContext, cfg: PolicyCfg) -> Verdict | None:
    for label in (ctx.form_text or "").split("\n"):
        if ph := stop_phrase_hit(label, cfg.stop_phrases):
            return Decision.needs_human, f"attestation: '{ph}' in field: {label.strip()[:120]}"
    if ph := stop_phrase_hit(ctx.text, cfg.stop_phrases, posting_text=True):
        return Decision.needs_human, f"attestation: '{ph}' in posting text"
    return None


# Default order: cheap hard filters first, human hand-off last (a skip should win over a human prompt).
GATES: dict[str, Gate] = {
    "employer": employer_gate,
    "title": title_gate,
    "years": years_gate,
    "graduation": graduation_gate,
    "sponsorship": sponsorship_gate,
    "citizenship": citizenship_gate,
    "salary": salary_gate,
    "stop_phrases": stop_phrase_gate,
}


def register_gate(name: str, fn: Gate) -> Gate:
    """Add a custom gate (enable it by listing its name in policy.gates)."""
    GATES[name] = fn
    return fn


def evaluate(ctx: GateContext, cfg: PolicyCfg, gates: list[str] | None = None) -> GateResult:
    names = gates if gates is not None else (cfg.gates if cfg.gates is not None else list(GATES))
    unknown = [n for n in names if n not in GATES]
    if unknown:
        raise ValueError(f"unknown gate(s) {unknown}; known: {sorted(GATES)}")
    skips: list[str] = []
    humans: list[str] = []
    for n in names:
        v = GATES[n](ctx, cfg)
        if v is None:
            continue
        (skips if v[0] == Decision.skip else humans).append(v[1])
    if skips:
        return GateResult(decision=Decision.skip, reasons=skips + humans)
    if humans:
        return GateResult(decision=Decision.needs_human, reasons=humans)
    return GateResult(decision=Decision.submit)


def evaluate_posting(p: Posting, cfg: PolicyCfg, form_text: str = "", graduation: str = "") -> GateResult:
    return evaluate(GateContext.from_posting(p, form_text, graduation), cfg)

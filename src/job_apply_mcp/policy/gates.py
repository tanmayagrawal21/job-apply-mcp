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

    @classmethod
    def from_posting(cls, p: Posting, form_text: str = "") -> GateContext:
        return cls(p.company, p.title, p.location, p.salary or "", p.description or "", form_text,
                   dict(p.flags))


# -- local fallback flagger ---------------------------------------------------------------------
_FLAG_PATTERNS: dict[str, str] = {
    "no_sponsorship": r"(?:will|do|does|can)\s*(?:not|n't)\s+(?:provide\s+|offer\s+)?(?:visa\s+)?sponsor"
                      r"|unable to (?:provide\s+)?(?:visa\s+)?sponsor|no (?:visa )?sponsorship"
                      r"|without (?:the need for )?(?:visa )?sponsorship|not (?:eligible|available) for sponsorship"
                      r"|sponsorship (?:is )?not (?:available|offered|provided)"
                      r"|must be (?:legally )?authorized to work[^.]{0,60}without",
    "citizenship": r"u\.?s\.?\s+citizen(?:ship)?|united states citizen(?:ship)?|citizenship (?:is )?required",
    "clearance": r"security clearance|\bts/sci\b|top secret|secret clearance|active clearance|polygraph",
    "us_person": r"\bitar\b|u\.?s\.?[ -]person|export[- ]control|\bear\b regulations",
}


def local_flags(text: str) -> dict[str, list[str]]:
    """Minimal fallback for the shared flagging helper. Returns {flag: [matched snippets]}."""
    out: dict[str, list[str]] = {}
    for k, pat in _FLAG_PATTERNS.items():
        hits = [m.group(0).strip() for m in re.finditer(pat, text, re.IGNORECASE)]
        if hits:
            out[k] = hits[:3]
    return out


def _flags(ctx: GateContext) -> dict[str, list[str]]:
    merged = dict(local_flags(ctx.text))
    for k, v in (ctx.flags or {}).items():
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


_MONTHS = {m: i for i, m in enumerate(
    ["jan", "feb", "mar", "apr", "may", "jun", "jul", "aug", "sep", "oct", "nov", "dec"], 1)}
_MON = r"(jan|feb|mar|apr|may|jun|jul|aug|sep|oct|nov|dec)[a-z]*\.?"
_DATE = re.compile(rf"\b{_MON}\s*,?\s*(20\d\d)\b", re.IGNORECASE)
_CLASS_OF = re.compile(r"class of\s*(20\d\d)|graduat\w*\s+(?:in|by|before|during|between)?\s*(?:the\s+)?"
                       r"(?:(?:spring|summer|fall|winter)\s+)?(20\d\d)\b", re.IGNORECASE)


def _ym(s: str) -> tuple[int, int] | None:
    m = re.fullmatch(r"\s*(\d{4})-(\d{1,2})\s*", s or "")
    return (int(m.group(1)), int(m.group(2))) if m else None


def graduation_gate(ctx: GateContext, cfg: PolicyCfg) -> Verdict | None:
    grad = _ym(cfg.graduation or "")
    if not grad:
        return None
    gi = grad[0] * 12 + grad[1]
    flex = cfg.graduation_flex_months
    text = ctx.text + " " + ctx.title
    # explicit window: "graduating between December 2025 and August 2026" / "Dec 2025 - Aug 2026"
    for m in re.finditer(r"graduat\w*|degree|class of|expected", text, re.IGNORECASE):
        seg = text[max(0, m.start() - 120): m.end() + 200]
        dates = [(int(y), _MONTHS[mo[:3].lower()]) for mo, y in _DATE.findall(seg)]
        if len(dates) >= 2:
            lo, hi = sorted(y * 12 + mo for y, mo in dates[:2])
            if not (lo - flex <= gi <= hi + flex):
                return Decision.skip, "grad date outside posting window"
            return None
    m = _CLASS_OF.search(text)
    if m:
        year = int(m.group(1) or m.group(2))
        lo, hi = year * 12 + 1, year * 12 + 12
        if not (lo - flex <= gi <= hi + flex):
            return Decision.skip, f"wants class of {year}"
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


# ADR-0040: a stop phrase matches whole words within one segment (one form label / option, or one description
# sentence). "no ai" no longer matches "no aim", "techno aid" or two adjacent form labels, and a requirement statement
# ("no AI experience required") is not an attestation. Real attestations ("personally completed", "my own work",
# "no AI allowed", "without the use of AI") still stop the application.
_REQUIREMENT_AFTER = re.compile(r"^[ \t/,-]*(?:/?ml\b|/?machine learning\b)?[ \t/,-]*(?:prior |previous |professional )?"
                                r"(?:experience|background|knowledge|expertise|skills?|degree|required|needed|necessary)\b",
                                re.IGNORECASE)


def _phrase_rx(phrase: str) -> re.Pattern[str] | None:
    words = re.findall(r"[a-z0-9]+", phrase.lower())
    if not words:
        return None
    return re.compile(r"(?<![a-z0-9])" + r"[ \t_/-]+".join(map(re.escape, words)) + r"(?![a-z0-9])", re.IGNORECASE)


def stop_phrase_hit(text: str, phrases: list[str]) -> str | None:
    """The first stop phrase found as whole words in one segment of `text`, else None."""
    segments = [x for x in re.split(r"[\n.!?;]+", text or "") if x.strip()]
    for ph in phrases:
        rx = _phrase_rx(ph)
        if rx is None:
            continue
        for seg in segments:
            for m in rx.finditer(seg):
                if not _REQUIREMENT_AFTER.match(seg[m.end():]):
                    return ph
    return None


def stop_phrase_gate(ctx: GateContext, cfg: PolicyCfg) -> Verdict | None:
    for label in (ctx.form_text or "").split("\n"):
        if ph := stop_phrase_hit(label, cfg.stop_phrases):
            return Decision.needs_human, f"attestation: '{ph}' in field: {label.strip()[:120]}"
    if ph := stop_phrase_hit(ctx.text, cfg.stop_phrases):
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


def evaluate_posting(p: Posting, cfg: PolicyCfg, form_text: str = "") -> GateResult:
    return evaluate(GateContext.from_posting(p, form_text), cfg)

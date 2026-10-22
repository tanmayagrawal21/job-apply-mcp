"""Pure text-flagging helpers for job descriptions (ADR-0007). No I/O, no config, no third-party imports,
so the policy component can reuse them directly.

Flags are *signals*, not decisions: each helper returns the matched snippets (or parsed numbers) and the
caller's policy decides what to do with them.
"""
from __future__ import annotations

import html
import re
from dataclasses import dataclass

# --------------------------------------------------------------------------------------- text helpers

_TAG = re.compile(r"<[^>]+>")
_BLOCK = re.compile(r"</?(?:p|br|li|ul|ol|div|h[1-6]|tr)\b[^>]*>", re.IGNORECASE)
_WS = re.compile(r"\s+")


def html_to_text(raw: str | None) -> str:
    """HTML (possibly entity-escaped, as Greenhouse returns it) -> single-spaced plain text."""
    if not raw:
        return ""
    s = html.unescape(raw)
    if "&lt;" in s or "&amp;lt;" in s:  # double-escaped
        s = html.unescape(s)
    s = _BLOCK.sub(" ", s)
    return _WS.sub(" ", html.unescape(_TAG.sub(" ", s))).strip()


# ------------------------------------------------------------------------------------------- patterns

_PATTERNS: dict[str, re.Pattern[str]] = {
    "no_sponsorship": re.compile(
        r"(?:unable|not able|cannot|can't|do not|does not|will not|won't|not (?:currently )?(?:able|willing|in a position))"
        r"\s+(?:to\s+)?(?:provide\s+|offer\s+|support\s+)?(?:visa\s+)?sponsor"
        r"|without (?:the need for )?(?:visa )?sponsorship|no (?:visa )?sponsorship"
        r"|not eligible for (?:visa )?sponsorship|sponsorship (?:is )?not (?:available|offered|provided)",
        re.IGNORECASE),
    "opt_excluded": re.compile(
        r"(?:F-?1|OPT|STEM OPT|I-983|CPT)[^.]{0,80}(?:not|unable|ineligible)|not open to (?:F-?1|J-?1)", re.IGNORECASE),
    "citizenship": re.compile(
        r"U\.?S\.? citizen(?:ship)? (?:is )?required|must be a U\.?S\.? citizen|security clearance"
        r"|active clearance|TS/SCI|ITAR|export[- ]control", re.IGNORECASE),
    "grad_window": re.compile(
        r"graduat\w*[^.]{0,80}?20\d\d|(?:class|graduates?) of\s*20\d\d|20\d\d\s+(?:new\s+)?(?:grad|graduate)s?\b", re.IGNORECASE),
    "phd_only": re.compile(r"PhD (?:required|only)|currently pursuing a PhD|New PhD Graduate", re.IGNORECASE),
    "academic": re.compile(
        r"\b(?:university|college|school of medicine|research institute|academic medical center)\b", re.IGNORECASE),
}

_YEARS = re.compile(
    r"(?<![\d,.$])(\d{1,2})\s*\+?\s*(?:(?:-|–|—|to)\s*\d{1,2}\s*)?(?:years?|yrs?)\b[^.\n]{0,60}?experience", re.IGNORECASE)
_YEARS_LEAD = re.compile(
    r"(?:minimum|at least|min\.?)\s+(?:of\s+)?(\d{1,2})\s*\+?\s*(?:years?|yrs?)\b", re.IGNORECASE)


def _hits(pat: re.Pattern[str], text: str, max_hits: int, hit_len: int) -> list[str]:
    out: list[str] = []
    for m in pat.finditer(text):
        s = m.group(0)[:hit_len]
        if s not in out:
            out.append(s)
        if len(out) >= max_hits:
            break
    return out


# ------------------------------------------------------------------------------------- years of exp

def years_mentions(text: str) -> list[int]:
    """Lower bound of every 'N[+|-M] years ... experience' mention, in order of appearance."""
    vals = [int(m.group(1)) for m in _YEARS.finditer(text)]
    vals += [int(m.group(1)) for m in _YEARS_LEAD.finditer(text)]
    return [v for v in vals if 0 < v <= 40]


def years_required(text: str) -> int | None:
    """Smallest stated experience requirement (the most lenient reading), or None when none is stated."""
    v = years_mentions(text)
    return min(v) if v else None


# ------------------------------------------------------------------------------------- sponsorship

def sponsorship_flags(text: str, max_hits: int = 3, hit_len: int = 140) -> dict[str, list[str]]:
    return {k: h for k in ("no_sponsorship", "opt_excluded") if (h := _hits(_PATTERNS[k], text, max_hits, hit_len))}


def citizenship_flags(text: str, max_hits: int = 3, hit_len: int = 140) -> list[str]:
    """Citizenship / clearance / export-control restrictions."""
    return _hits(_PATTERNS["citizenship"], text, max_hits, hit_len)


# ----------------------------------------------------------------------------------- grad window

_SEASON_MONTH = {"spring": 5, "may": 5, "summer": 7, "fall": 9, "autumn": 9, "winter": 12, "december": 12, "dec": 12,
                 "june": 6, "july": 7, "august": 8, "aug": 8, "september": 9, "sept": 9, "sep": 9,
                 "october": 10, "oct": 10, "november": 11, "nov": 11, "january": 1, "jan": 1,
                 "february": 2, "feb": 2, "march": 3, "mar": 3, "april": 4, "apr": 4}
_SEASON_RE = re.compile(r"\b(" + "|".join(sorted(_SEASON_MONTH, key=len, reverse=True)) + r")\b\.?\s*(?:of\s*)?(20\d\d)?", re.IGNORECASE)


def grad_windows(text: str, max_hits: int = 3, hit_len: int = 140) -> list[str]:
    return _hits(_PATTERNS["grad_window"], text, max_hits, hit_len)


def _parse_window(s: str) -> tuple[int, int | None] | None:
    years = [int(y) for y in re.findall(r"20\d\d", s)]
    if not years:
        return None
    year = max(years)
    month = None
    for m in _SEASON_RE.finditer(s):
        if m.group(2) and int(m.group(2)) == year or not m.group(2):
            month = _SEASON_MONTH[m.group(1).lower()]
    return year, month


def grad_window_conflict(windows: list[str], graduation: str) -> bool:
    """True when every graduation window in the posting starts after `graduation` ('YYYY-MM').

    Windows that mention nothing parseable are ignored. A posting that mentions both a later and an
    on-or-before window is not a conflict (it is open to both).
    """
    try:
        gy, gm = (int(x) for x in graduation.split("-")[:2])
    except ValueError:
        return False
    later = not_later = False
    for w in windows:
        parsed = _parse_window(w)
        if not parsed:
            continue
        y, m = parsed
        if y > gy or (y == gy and m is not None and m > gm):
            later = True
        else:
            not_later = True
    return later and not not_later


# --------------------------------------------------------------------------------------------- pay

_CUR = r"(?:\$|USD\s?|US\$|£|€|CAD\s?|C\$)"
_NUM = r"\d{1,3}(?:,\d{3})+(?:\.\d+)?|\d+(?:\.\d+)?"
_SAL = re.compile(
    rf"(?P<cur>{_CUR})\s?(?P<a>{_NUM})\s?(?P<ak>[kK])?\s*(?:-|–|—|to)\s*(?:{_CUR})?\s?(?P<b>{_NUM})\s?(?P<bk>[kK])?"
    r"(?P<per>\s*(?:/|per\s|an\s|a\s)\s*(?:hr|hour|yr|year|annum|month|mo)\b)?", re.IGNORECASE)
_CUR_CODE = {"$": "USD", "usd": "USD", "us$": "USD", "£": "GBP", "€": "EUR", "cad": "CAD", "c$": "CAD"}


@dataclass(frozen=True)
class SalaryRange:
    min: float
    max: float
    period: str = "year"   # year | hour | month
    currency: str = "USD"
    raw: str = ""

    def annual(self) -> tuple[float, float]:
        mult = {"year": 1, "hour": 2080, "month": 12}[self.period]
        return self.min * mult, self.max * mult

    def summary(self) -> str:
        sym = {"USD": "$", "GBP": "£", "EUR": "€", "CAD": "C$"}.get(self.currency, self.currency + " ")
        unit = {"year": "", "hour": "/hr", "month": "/mo"}[self.period]
        return f"{sym}{self.min:,.0f}-{sym}{self.max:,.0f}{unit}"


def extract_salary(text: str) -> SalaryRange | None:
    """First plausible pay range in the text ('$120,000 - $150,000', '$95k-$120k', '$45 to $60 per hour')."""
    for m in _SAL.finditer(text):
        a, b = float(m["a"].replace(",", "")), float(m["b"].replace(",", ""))
        if m["bk"] or m["ak"]:
            a = a * 1000 if (m["ak"] or a < 1000) else a
            b = b * 1000 if (m["bk"] or b < 1000) else b
        if a > b:
            continue
        per = (m["per"] or "").lower()
        period = "hour" if re.search(r"hr|hour", per) else "month" if re.search(r"mo", per) else "year"
        if not per:
            period = "hour" if b < 500 else "year"
        floor = 8 if period == "hour" else 1000
        if a < floor or (period == "year" and b < 10_000):
            continue
        cur = _CUR_CODE.get(m["cur"].strip().lower(), "USD")
        return SalaryRange(a, b, period, cur, m.group(0).strip())
    return None


# ------------------------------------------------------------------------------------------ combined

def flag_text(text: str, *, max_hits: int = 3, hit_len: int = 140) -> dict[str, list[str]]:
    """All flags found in `text`: {flag: [snippets]}. Empty dict when clean.

    Keys: no_sponsorship, opt_excluded, citizenship, years, grad_window, phd_only, academic.
    'years' snippets are the matched phrases; use `years_required` for the number.
    """
    out: dict[str, list[str]] = {}
    for k, pat in _PATTERNS.items():
        if h := _hits(pat, text, max_hits, hit_len):
            out[k] = h
    if h := _hits(_YEARS, text, max_hits, hit_len):
        out["years"] = h
    return out

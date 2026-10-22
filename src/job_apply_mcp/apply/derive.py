"""Pure derivations used by the mapper (ADR-0030): experience years, year-range options, language picks.

No I/O and no profile access; everything takes plain values so it is trivially unit-testable.
"""
from __future__ import annotations

import math
import re
from collections.abc import Iterable
from datetime import date, datetime

_YM = re.compile(r"(\d{4})-(\d{1,2})")


def _month_index(ym: str, default: date | None = None) -> int | None:
    m = _YM.fullmatch((ym or "").strip())
    if m:
        return int(m.group(1)) * 12 + int(m.group(2)) - 1
    if default is not None:
        return default.year * 12 + default.month - 1
    return None


def total_years(spans: Iterable[tuple[str, str]], today: date | None = None) -> float | None:
    """Years covered by the union of (start 'YYYY-MM', end 'YYYY-MM' or '' = current) spans. Overlaps are counted
    once. Spans without a parseable start are ignored; None when nothing is usable (never a guess)."""
    today = today or datetime.now().astimezone().date()
    iv: list[tuple[int, int]] = []
    for start, end in spans:
        a = _month_index(start)
        b = _month_index(end, today) if (end or "").strip() == "" else _month_index(end)
        if a is not None and b is not None and b >= a:
            iv.append((a, b + 1))        # inclusive of the end month
    if not iv:
        return None
    iv.sort()
    months, (cs, ce) = 0, iv[0]
    for s, e in iv[1:]:
        if s <= ce:
            ce = max(ce, e)
        else:
            months += ce - cs
            cs, ce = s, e
    months += ce - cs
    return months / 12


def whole_years(years: float) -> int:
    """Floor, never round up (a truthful lower bound)."""
    return max(0, math.floor(years + 1e-9))


_RANGE = re.compile(r"(\d+)\s*(?:-|–|—|to)\s*(\d+)")
_PLUS = re.compile(r"(\d+)\s*\+|(?:more than|over|at least|minimum(?: of)?)\s*(\d+)", re.IGNORECASE)
_LESS = re.compile(r"(?:less than|under|fewer than|<)\s*(\d+)", re.IGNORECASE)
_NONE = re.compile(r"^\s*(none|no experience|0|zero)\b", re.IGNORECASE)


def _bounds(option: str) -> tuple[int, float] | None:
    """(lo, hi) inclusive whole years for an option such as '3-5 years', '5+', 'Less than 1', 'None'."""
    o = option.strip()
    if (m := _LESS.search(o)):
        return 0, int(m.group(1)) - 1
    if (m := _RANGE.search(o)):
        lo, hi = int(m.group(1)), int(m.group(2))
        return (lo, hi) if hi >= lo else None
    if (m := _PLUS.search(o)):
        return int(m.group(1) or m.group(2)), math.inf
    if _NONE.search(o):
        return 0, 0
    if (m := re.fullmatch(r"\D*(\d+)\D*years?\D*", o)):
        return int(m.group(1)), int(m.group(1))
    return None


def years_option(years: float, options: list[str]) -> str | None:
    """The option whose range holds floor(years). Several matches -> the one with the highest lower bound that is
    still <= years (true, and never rounds up); no parseable ranges or no match -> None."""
    y = whole_years(years)
    hits = [(b[0], o) for o in options if (b := _bounds(o)) and b[0] <= y <= b[1]]
    if not hits:
        return None
    top = max(lo for lo, _ in hits)
    best = [o for lo, o in hits if lo == top]
    return best[0] if len(best) == 1 else None


_ALIAS = {"golang": "go", "cpp": "c++", "c plus plus": "c++", "js": "javascript", "ts": "typescript",
          "py": "python", "node": "node.js", "nodejs": "node.js"}


def lang_key(s: str) -> str:
    k = re.sub(r"\s+", " ", s.strip().lower())
    return _ALIAS.get(k, k)


def pick_languages(wanted: list[str], options: list[str], top: int) -> list[str]:
    """First `top` profile languages that are present among the options (case/alias-insensitive, exact only)."""
    by = {lang_key(o): o for o in options}
    out: list[str] = []
    for w in wanted:
        o = by.get(lang_key(w))
        if o and o not in out:
            out.append(o)
        if len(out) >= max(1, top):
            break
    return out

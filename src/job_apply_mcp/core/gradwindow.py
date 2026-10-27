"""Graduation windows stated in posting text (ADR-0059). Pure text, no I/O: used by the search prefilter
(platforms.textflags) and the graduation policy gate alike, so both read a posting the same way."""
from __future__ import annotations

import re

_SEASON_MONTH = {"spring": 5, "may": 5, "summer": 7, "fall": 9, "autumn": 9, "winter": 12, "december": 12, "dec": 12,
                 "june": 6, "jun": 6, "jul": 7, "july": 7, "august": 8, "aug": 8, "september": 9, "sept": 9, "sep": 9,
                 "october": 10, "oct": 10, "november": 11, "nov": 11, "january": 1, "jan": 1,
                 "february": 2, "feb": 2, "march": 3, "mar": 3, "april": 4, "apr": 4}
_RANGE_SEP = re.compile(r"^\s*(?:-|–|—|to|and|through|until|thru)\s*$", re.IGNORECASE)
_UPPER = re.compile(r"\b(?:by|before|no later than|prior to|until|on or before)\s*$", re.IGNORECASE)
_LOWER = re.compile(r"\b(?:after|since|from|on or after|starting)\s*$", re.IGNORECASE)
_OR_EARLIER = re.compile(r",? *or (?:earlier|before|sooner)\b", re.IGNORECASE)
_OR_LATER = re.compile(r",? *(?:or (?:later|after)|and (?:later|beyond)|onwards?)\b", re.IGNORECASE)
_DATE_TOK = re.compile(r"(?:\b(" + "|".join(sorted(_SEASON_MONTH, key=len, reverse=True)) + r")\b\.?,?\s*(?:of\s+)?)?"
                       r"\b(20\d\d)\b", re.IGNORECASE)


def _tok_months(m: re.Match[str]) -> tuple[int, int]:
    y, mon = int(m.group(2)), _SEASON_MONTH.get((m.group(1) or "").lower())
    return y * 12 + (mon or 1), y * 12 + (mon or 12)


def grad_ranges(text: str) -> list[tuple[int | None, int | None]]:
    """ADR-0059: every graduation window a posting text states, as (first, last) month indices (year*12+month; None =
    open). "class of 2028" -> 2028-01..2028-12; "between Dec 2025 and Aug 2026" -> one range; "by June 2028" /
    "2028 or earlier" -> no lower bound; "2026, 2027 or 2028" -> three windows (any one fits)."""
    out: list[tuple[int | None, int | None]] = []
    for seg in re.split(r"(?<=[A-Za-z0-9]{2})[.!?;](?=\s|$)|\n", text or ""):
        if not re.search(r"graduat|class of|\bgrads?\b", seg, re.IGNORECASE):
            continue
        toks = list(_DATE_TOK.finditer(seg))
        i = 0
        while i < len(toks):
            m = toks[i]
            lo, hi = _tok_months(m)
            if i + 1 < len(toks) and _RANGE_SEP.match(seg[m.end():toks[i + 1].start()]):
                out.append((lo, _tok_months(toks[i + 1])[1]))
                i += 2
                continue
            before, after = seg[max(0, m.start() - 20):m.start()], seg[m.end():m.end() + 20]
            if _UPPER.search(before) or _OR_EARLIER.match(after):
                out.append((None, hi))
            elif _LOWER.search(before) or _OR_LATER.match(after):
                out.append((lo, None))
            else:
                out.append((lo, hi))
            i += 1
    return out


def grad_mismatch(text: str, graduation: str, flex_months: int = 0) -> bool:
    """True only on a clear mismatch: the text states at least one graduation window and `graduation` ('YYYY-MM'),
    widened by `flex_months` on both sides, fits none of them. Unparseable text or dates are never a mismatch."""
    m = re.fullmatch(r"\s*(\d{4})-(\d{1,2})\s*", graduation or "")
    if not m:
        return False
    g, flex = int(m.group(1)) * 12 + int(m.group(2)), max(0, int(flex_months or 0))
    ranges = grad_ranges(text)
    return bool(ranges) and not any((lo is None or lo - flex <= g) and (hi is None or g <= hi + flex)
                                    for lo, hi in ranges)


def grad_window_conflict(windows: list[str], graduation: str, flex_months: int = 0) -> bool:
    """True when the posting's graduation windows (`grad_window` flag snippets) clearly exclude `graduation`
    ('YYYY-MM') widened by `flex_months` (see grad_mismatch)."""
    return grad_mismatch("\n".join(windows), graduation, flex_months)

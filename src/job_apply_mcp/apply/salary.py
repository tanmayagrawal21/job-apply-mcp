"""Deterministic salary rule (ADR-0011 Amendment 2). No LLM, no network; every parameter is in PolicyCfg.

floor = base_salary * col_multiplier(posting location)
range posted: ask = min(max(anchor, floor), top), anchor = midpoint, or lower-middle when the range spans levels
no range:     ask = max(floor, default_ask)
The ask never exceeds the posted top and is rounded to `salary_round_to`. The computation is returned for audit.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any

from ..config import PolicyCfg
from ..core.models import FieldKind, FormField, Posting
from ..policy.gates import parse_salary

_SALARY_LABEL = re.compile(r"salary|compensation|pay (?:expectation|requirement)|expected pay|desired pay",
                           re.IGNORECASE)
_NOT_ASK = re.compile(r"history|current(?:ly)? (?:salary|compensation)|previous|last (?:salary|drawn)|"
                      r"how much (?:do|did) you (?:make|earn)|consent|bonus", re.IGNORECASE)


def is_salary_field(f: FormField) -> bool:
    return f.kind != FieldKind.file and bool(_SALARY_LABEL.search(f.label)) and not _NOT_ASK.search(f.label)


def col_multiplier(cfg: PolicyCfg, location: str) -> float:
    loc = (location or "").lower()
    for sub, m in cfg.col_multipliers.items():
        if sub.lower() in loc:
            return m
    return 1.0


def _round(v: float, step: int, top: float | None) -> int:
    step = max(1, step)
    r = int(round(v / step) * step)
    if top is not None and r > top:
        r = int(top // step * step) if top >= step else int(top)
    return r


@dataclass
class SalaryDecision:
    ask: int
    audit: dict[str, Any]


def compute_ask(cfg: PolicyCfg, posting: Posting) -> SalaryDecision | None:
    """None when there is nothing to compute from (no base salary, no range, no default ask)."""
    base = cfg.base_salary if cfg.base_salary is not None else cfg.salary_floor
    mult = col_multiplier(cfg, posting.location)
    floor = base * mult if base is not None else None
    rng = parse_salary(posting.salary or "", cfg.hours_per_year)
    audit: dict[str, Any] = {"base": base, "col_multiplier": mult, "floor": floor, "range": list(rng) if rng else None,
                             "location": posting.location}
    if rng:
        lo, hi = rng
        multi = lo > 0 and hi / lo > cfg.salary_multi_level_ratio
        anchor = lo + (hi - lo) * (cfg.salary_lower_middle if multi else 0.5)
        raw = max(anchor, floor or 0)
        ask = _round(min(raw, hi), cfg.salary_round_to, hi)
        audit.update(rule="range", anchor=anchor, anchor_kind="lower_middle" if multi else "midpoint", cap=hi)
    else:
        cands = [x for x in (floor, cfg.default_ask) if x is not None]
        if not cands:
            return None
        ask = _round(max(cands), cfg.salary_round_to, None)
        audit.update(rule="no_range", default_ask=cfg.default_ask)
    audit["ask"] = ask
    return SalaryDecision(ask, audit)


def format_ask(f: FormField, ask: int, currency_symbol: str = "$") -> str | None:
    """number field -> '115000'; text -> '$115,000'; select -> the option whose range contains the ask."""
    if f.options:
        for o in f.options:
            r = parse_salary(o)
            if r and r[0] <= ask <= r[1]:
                return o
        return None
    if f.kind == FieldKind.number:
        return str(ask)
    return f"{currency_symbol}{ask:,}"

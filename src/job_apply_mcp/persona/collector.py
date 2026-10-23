"""Gap collector: records every unresolved field and every default-used answer of a plan (ADR-0047)."""
from __future__ import annotations

from typing import Any

from ..apply.mapper import DEMOGRAPHIC_KEYS, canonical_key
from ..apply.salary import is_salary_field
from ..core.models import FieldKind
from ..policy.gates import parse_salary
from .gaps import ProfileDB
from .guard import CLAIMABLE_KEYS, blocked_label, is_demographic_key, plain_fact_label
from .keys import gap_key, is_exceptional_label

# canonical authorization-type keys a human may be asked about (their answers need origin user_written)
_ASKABLE = {"work_authorized", "requires_sponsorship", "us_citizen", "citizenship", "willing_to_relocate"}


def collect_gaps(db: ProfileDB, policy: Any, profile: Any, *, ref: str, posting: Any, fields: list[Any],
                 answers: list[Any], unresolved: list[str]) -> int:
    """Returns the number of gap rows written. Idempotent per (key, ref). Never raises on odd input."""
    by_key = {f.key: f for f in fields}
    seen: dict[str, tuple[str, str, bool, str]] = {}      # gap key -> (question, kind, blocked, default_used)

    def add(key: str, question: str, kind: str, blocked: bool = False, default: str = "") -> None:
        old = seen.get(key)
        if old is None or (blocked and not old[2]) or (kind == "unresolved" and old[1] != "unresolved"):
            seen[key] = (question, kind, blocked, default)

    for k in unresolved:
        f = by_key.get(k)
        if f is None or f.kind == FieldKind.file:
            continue
        ck = canonical_key(f.label)
        if blocked_label(f.label) and not (ck and not is_demographic_key(ck) and ck in _ASKABLE):
            continue                        # attestations, consents, legal and EEO questions are never collected
        gk = gap_key(f.label, ck)
        if ck is None and not (gk in CLAIMABLE_KEYS or gk.startswith("persona.") or plain_fact_label(f.label)):
            continue                        # nothing could be applied from an answer: do not ask
        add(gk, f.label, "unresolved", bool(f.required))

    has_range = bool(parse_salary(getattr(posting, "salary", "") or ""))
    for a in answers:
        f = by_key.get(a.key)
        if f is None:
            continue
        ck = canonical_key(f.label)
        src = a.source or ""
        val = str(a.value)[:120]
        if ck in DEMOGRAPHIC_KEYS:
            continue                        # EEO answers come from [demographics] only; the default is not a gap
        elif src.startswith("llm:"):
            if blocked_label(f.label) or not plain_fact_label(f.label):
                continue
            add(gap_key(f.label, ck), f.label, "default", False, f"model guess: {val}")
        elif ck == "years_experience" and policy is not None and not policy.configured("experience_counting"):
            add("persona.experience_counting", "Which roles count as professional experience? Answer full_time_only, include_internships or all.",
                "default", False, "full-time roles only")
        elif is_salary_field(f) and not has_range and not src.startswith("persona") and policy is not None \
                and not policy.configured("salary_policy") or src == "rule:salary" and not has_range and policy is not None and not policy.configured("salary_policy"):
            add("persona.salary_no_range", "What annual figure should be entered when a posting shows no pay range?",
                "default", False, val)
    if policy is not None and not policy.projects():
        for f in fields:
            if is_exceptional_label(f.label):
                add("persona.exceptional_work", "Which project is your strongest example of exceptional work "
                    "(name: one-line summary)?", "unresolved" if f.key in unresolved else "default", f.required)
                break
    db.clear_ref(ref, set(seen))
    for key, (q, kind, blocked, default) in seen.items():
        db.record_hit(key, q, ref=ref, kind=kind, blocked=blocked, default_used=default,
                      company=getattr(posting, "company", ""), title=getattr(posting, "title", ""))
    return len(seen)

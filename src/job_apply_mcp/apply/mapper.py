"""Deterministic field -> answer mapping. No network, no LLM; everything here is unit-testable.

Order of resolution per field:
  1. file fields map to the resume / cover letter by label
  2. profile standard answer by canonical key (via label aliases), then by normalized label
  3. for selects, the answer is snapped to the best option (exact, containment, fuzzy, decline synonyms)
Anything left over is returned as unresolved for the LLM tier / client.
"""
from __future__ import annotations

import difflib
import re
from dataclasses import dataclass, field
from typing import Any

from ..core.models import Answer, FieldKind, FormField
from .ports import ProfileLike

_PUNCT = re.compile(r"[^a-z0-9 ]+")
_OPTIONAL = re.compile(r"\((optional|required)\)|\b(optional|required)\b$")


def normalize(label: str) -> str:
    s = label.lower().replace("’", "'")
    s = _OPTIONAL.sub(" ", s)
    s = _PUNCT.sub(" ", s)
    return re.sub(r"\s+", " ", s).strip()


# canonical profile key -> normalized-label patterns (searched as whole-phrase regexes, first match wins)
ALIASES: list[tuple[str, tuple[str, ...]]] = [
    ("first_name", (r"^(legal )?first name$", r"^given name$")),
    ("last_name", (r"^(legal )?(last|family) name$", r"^surname$")),
    ("full_name", (r"^(full |legal )?name$",)),
    ("preferred_name", (r"^preferred (first )?name$",)),
    ("email", (r"^e ?mail( address)?$",)),
    ("phone", (r"^(mobile |cell )?phone( number)?$", r"^mobile( number)?$")),
    ("linkedin", (r"linkedin",)),
    ("github", (r"github",)),
    ("website", (r"^(personal )?(website|portfolio)( url)?$", r"^website$", r"^portfolio")),
    ("location", (r"^(current )?location( city)?$", r"^city$", r"^current city$")),
    ("country", (r"^country( of residence)?$",)),
    ("current_company", (r"^current (company|employer)$",)),
    ("current_title", (r"^current (job )?title$",)),
    ("work_authorized", (r"authorized to work", r"legally authorized", r"eligible to work", r"work authorization")),
    ("requires_sponsorship", (r"sponsorship", r"require.* visa")),
    ("willing_to_relocate", (r"relocat",)),
    ("start_date", (r"(earliest )?start date", r"available to start", r"notice period")),
    ("salary_expectation", (r"salary expectation", r"expected (salary|compensation)", r"desired (salary|compensation)")),
    ("referral_source", (r"how did you (hear|find)", r"source$")),
    ("gender", (r"^gender( identity)?$", r"^what is your gender")),
    ("hispanic", (r"hispanic|latino",)),
    ("race", (r"^race|ethnicity",)),
    ("veteran", (r"veteran",)),
    ("disability", (r"disabilit",)),
    ("pronouns", (r"pronoun",)),
    ("school", (r"^school( name)?$", r"^university$", r"^college$")),
    ("degree", (r"^degree$",)),
    ("discipline", (r"^(discipline|major|field of study)$",)),
    ("graduation", (r"graduat",)),
    ("zip", (r"^(zip|postal)( code)?$",)),
    ("over_18", (r"18 years|at least 18|legal age",)),
    ("privacy_ack", (r"privacy (policy|notice)", r"acknowledge",)),
]

DECLINE_MARKERS = ("decline", "prefer not", "don t wish", "do not wish", "do not want", "not to answer",
                   "rather not", "choose not", "not to disclose", "no answer")
_YES = {"yes", "true", "y", "1"}
_NO = {"no", "false", "n", "0"}


def canonical_key(label: str) -> str | None:
    n = normalize(label)
    for key, pats in ALIASES:
        if any(re.search(p, n) for p in pats):
            return key
    return None


def best_option(value: Any, options: list[str], *, cutoff: float = 0.78) -> str | None:
    """Snap `value` onto one of `options` or return None. Never invents an option."""
    if not options:
        return None
    nopts = {normalize(o): o for o in options if normalize(o)}
    sv = str(value).strip()
    nv = normalize(sv)
    if nv in ("decline", "prefer not to say") or nv.startswith("decline"):
        for n, o in nopts.items():
            if any(m in n for m in DECLINE_MARKERS):
                return o
        return None
    if nv in nopts:
        return nopts[nv]
    if nv in _YES | _NO:
        want = nv in _YES
        for n, o in nopts.items():
            if n.split(" ")[0] in (_YES if want else _NO):
                return o
    # containment, preferring the tightest (shortest) container; require word-level overlap
    cands = [o for n, o in nopts.items() if nv and (re.search(rf"\b{re.escape(nv)}\b", n) or n in nv)]
    if cands:
        return min(cands, key=len)
    m = difflib.get_close_matches(nv, list(nopts), n=1, cutoff=cutoff)
    return nopts[m[0]] if m else None


def _file_for(label: str, profile: ProfileLike) -> str | None:
    n = normalize(label)
    if "cover" in n:
        return profile.cover_letter_path
    if re.search(r"resume|\bcv\b|curriculum", n):
        return profile.resume_path
    return None


def _coerce(f: FormField, value: Any) -> Any | None:
    if f.kind in (FieldKind.select,):
        return best_option(value, f.options)
    if f.kind == FieldKind.multiselect:
        vals = value if isinstance(value, list) else [value]
        out = [o for o in (best_option(v, f.options) for v in vals) if o]
        return out or None
    if f.kind == FieldKind.boolean:
        nv = normalize(str(value))
        return True if nv in _YES else False if nv in _NO else (value if isinstance(value, bool) else None)
    if f.kind in (FieldKind.text, FieldKind.textarea, FieldKind.location, FieldKind.date, FieldKind.number,
                  FieldKind.unknown):
        if f.options:  # unknown kind that actually has options
            return best_option(value, f.options)
        s = str(value)
        return s[: f.max_length] if f.max_length and len(s) > f.max_length else s
    return None


@dataclass
class MapResult:
    answers: list[Answer] = field(default_factory=list)
    unresolved: list[FormField] = field(default_factory=list)


def map_fields(fields: list[FormField], profile: ProfileLike) -> MapResult:
    res = MapResult()
    for f in fields:
        if f.kind == FieldKind.file:
            path = _file_for(f.label, profile)
            if path:
                res.answers.append(Answer(key=f.key, value=path, source="profile"))
            elif f.required:
                res.unresolved.append(f)
            continue
        value: Any | None = None
        ck = canonical_key(f.label)
        if ck:
            value = profile.lookup(ck)
        if value is None:
            value = profile.lookup(normalize(f.label))
        coerced = _coerce(f, value) if value is not None else None
        if coerced is None or coerced == "":
            res.unresolved.append(f)
        else:
            res.answers.append(Answer(key=f.key, value=coerced, source="profile" if ck is None else f"rule:{ck}"))
    return res

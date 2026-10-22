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
    ("sexual_orientation", (r"sexual orientation",)),
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


_STOP = {"i", "am", "a", "an", "the", "to", "of", "is", "are", "be", "or", "and", "in", "my", "me", "you",
         "your", "for", "at", "on", "do", "s"}
_NEGATORS = {"not", "no", "never", "unable", "cannot", "cant", "without", "none", "neither", "nor"}
_AFFIRM = {"yes", "able", "willing", "open", "can", "will", "authorized", "eligible", "true", "do", "have",
           "am", "would"}


def tokens(text: str) -> list[str]:
    """Lowercased word tokens; contractions with n't ("can't", "don't") become the negator "not"."""
    t = text.lower().replace("’", "'")
    t = re.sub(r"\b(\w+)n't\b", r"\1 not", t)
    t = re.sub(r"\bcannot\b", "can not", t)
    return re.findall(r"[a-z0-9]+", t)


def has_negator(text: str) -> bool:
    return any(w in _NEGATORS for w in tokens(text))


def polarity(text: str) -> int:
    """-1 negative (contains a negator), +1 explicitly affirmative, 0 neutral."""
    toks = tokens(text)
    if any(w in _NEGATORS for w in toks):
        return -1
    return 1 if toks and (toks[0] == "yes" or any(w in _AFFIRM for w in toks)) else 0


def _content(text: str) -> set[str]:
    return {w for w in tokens(text) if w not in _STOP}


def _yes_no_option(value_yes: bool, options: list[str]) -> str | None:
    """Pick the option whose polarity matches a yes/no answer; ambiguous or opposite-only -> None."""
    want = 1 if value_yes else -1
    cands = [o for o in options if polarity(o) == want]
    if value_yes:
        cands = [o for o in cands if polarity(o) == 1]
    if len(cands) > 1:
        lead = [o for o in cands if tokens(o)[:1] and tokens(o)[0] in ("yes", "no")]
        cands = lead or cands
    return cands[0] if len(cands) == 1 else None


def exact_option(value: Any, options: list[str]) -> str | None:
    """Near-exact only (equal after normalization, or equal/nested token sets of >=1 word). For demographic,
    pronoun and EEO answers, where a semantic guess is never acceptable."""
    nv = normalize(str(value))
    tv = set(tokens(str(value)))
    if not tv:
        return None
    hits = []
    for o in options:
        to = set(tokens(o))
        if not to or has_negator(o) != has_negator(str(value)):
            continue
        if normalize(o) == nv or to == tv or (len(to & tv) >= min(len(to), len(tv)) and min(len(to), len(tv)) >= 2):
            hits.append(o)
    return hits[0] if len(hits) == 1 else None


def decline_option(options: list[str]) -> str | None:
    for o in options:
        if any(m in normalize(o) for m in DECLINE_MARKERS):
            return o
    return None


def best_option(value: Any, options: list[str], *, cutoff: float = 0.78) -> str | None:
    """Snap `value` onto one of `options` or return None. Never invents an option and never crosses polarity
    (a "Yes"/profile-positive answer cannot select a negated option, and vice versa)."""
    if not options:
        return None
    nopts = {normalize(o): o for o in options if normalize(o)}
    sv = str(value).strip()
    nv = normalize(sv)
    if nv in ("decline", "prefer not to say") or nv.startswith("decline"):
        return decline_option(options)
    if nv in nopts:
        return nopts[nv]
    if nv in _YES | _NO:
        return _yes_no_option(nv in _YES, list(nopts.values()))
    vneg = has_negator(sv)
    vpol = polarity(sv)
    ok = {n: o for n, o in nopts.items() if has_negator(o) == vneg}
    if vpol == 1 and not vneg:  # affirmative sentence: never land on a negated option
        ok = {n: o for n, o in ok.items() if polarity(o) >= 0}
    # containment, preferring the tightest (shortest) container; require word-level overlap
    cands = [o for n, o in ok.items() if nv and (re.search(rf"\b{re.escape(nv)}\b", n) or n in nv)]
    if cands:
        return min(cands, key=len)
    cv = _content(sv)
    scored = [(difflib.SequenceMatcher(None, nv, n).ratio(), o) for n, o in ok.items() if cv & _content(o)]
    scored = [x for x in scored if x[0] >= cutoff]
    if not scored:
        return None
    scored.sort(key=lambda x: -x[0])
    if len(scored) > 1 and scored[0][0] - scored[1][0] < 0.03:
        return None  # ambiguous -> unresolved rather than a guess
    return scored[0][1]


# ---- fabricated / placeholder values (ADR-0011 amendment) ---------------------------------------
_FAKE = re.compile(
    r"(?:^|[\s/@.])(?:example|sample|test|domain|yourdomain|yoursite|mysite|foo|bar|placeholder|localhost)"
    r"\.(?:com|org|net|io|dev|local|test)\b|\bexample\.(?:com|org|net)\b|lorem ipsum|\bplaceholder\b|"
    r"\bxxx+\b|\btbd\b|\byour[-_ ]?(?:name|username|handle|email|site|website|url|link|company|profile)\b|"
    r"<[^<>]{1,40}>|\[(?:url|link|email|name|your[^\]]*)\]|"
    r"^(?:user|test|name|email|you|yourname|foo|john\.?doe|jane\.?doe)@|\b555[-. ]?01\d\d\b|"
    r"\b123[-. ]?456[-. ]?7890\b|\b(\d)\1{6,}\b|^@?(?:username|handle|user)$", re.IGNORECASE)


def looks_fabricated(value: Any) -> bool:
    """True for placeholder-ish URLs/emails/phones/usernames (example.com, user@..., 555-0100, ...)."""
    return bool(_FAKE.search(str(value).strip()))


# ---- derived fields -------------------------------------------------------------------------------
_GEO = re.compile(r"^(latitude|longitude|lat|lon|lng|long)$")
_GRAD = re.compile(r"graduat|expected completion|degree (completion|date)")
_MONTH_NAMES = ["january", "february", "march", "april", "may", "june", "july", "august", "september",
                "october", "november", "december"]


def geo_kind(f: FormField) -> str | None:
    """'lat' | 'lon' when the field is a hidden/derived coordinate field."""
    for t in (normalize(f.key), normalize(f.label)):
        m = _GEO.match(t.replace(" ", ""))
        if m:
            return "lat" if m.group(1).startswith("lat") else "lon"
    return None


def _term(month: int) -> str:
    return "winter" if month in (12, 1, 2) else "spring" if month <= 5 else "summer" if month <= 8 else "fall"


def derive_graduation(f: FormField, ym: str) -> str | None:
    """Deterministic answer from profile education end 'YYYY-MM'. Select -> only an option containing the
    year (and matching month or term when several), else None. Never returns a different year."""
    m = re.fullmatch(r"\s*(\d{4})-(\d{1,2})\s*", str(ym or ""))
    if not m:
        return None
    year, month = int(m.group(1)), int(m.group(2))
    if not 1 <= month <= 12:
        return None
    mname, term = _MONTH_NAMES[month - 1], _term(month)
    if f.options:
        yr = [o for o in f.options if re.search(rf"\b{year}\b", o)]
        if not yr:
            return None
        for key in (mname, mname[:3], term):
            hit = [o for o in yr if re.search(rf"\b{key}", o.lower())]
            if len(hit) == 1:
                return hit[0]
        return yr[0] if len(yr) == 1 else None
    lab = normalize(f.label)
    if f.kind == FieldKind.date:
        return f"{year}-{month:02d}"
    if f.kind == FieldKind.number or re.search(r"\byear\b", lab) and "month" not in lab:
        return str(year)
    return f"{mname.capitalize()} {year}"


DEMOGRAPHIC_KEYS = {"gender", "hispanic", "race", "veteran", "disability", "pronouns", "sexual_orientation"}
_DEMOGRAPHIC_LABEL = re.compile(r"gender|pronoun|ethnic|\brace\b|hispanic|latino|veteran|disabilit|"
                                r"sexual orientation|transgender|\beeo\b|self[- ]identif", re.IGNORECASE)
_REFERRED = re.compile(r"referr|employee|friend|colleague|coworker", re.IGNORECASE)
DEFAULT_REFERRAL_PREFERENCE = ["Job board", "Other job board", "Other", "LinkedIn"]


def pick_source(profile_value: Any, options: list[str], preference: list[str]) -> str | None:
    """'How did you hear' -> the profile value if given, else the first preference present. Referral/employee
    options are chosen only when the profile itself says the candidate was referred."""
    referred = bool(profile_value and _REFERRED.search(str(profile_value)))
    pool = options if referred else [o for o in options if not _REFERRED.search(o)]
    cands = ([str(profile_value)] if profile_value else []) + ([] if referred else list(preference))
    for c in cands:
        got = best_option(c, pool, cutoff=0.9)
        if got:
            return got
    return None


def _file_for(label: str, profile: ProfileLike) -> str | None:
    n = normalize(label)
    if "cover" in n:
        return profile.cover_letter_path
    if re.search(r"resume|\bcv\b|curriculum", n):
        return profile.resume_path
    return None


def _coerce(f: FormField, value: Any, cutoff: float = 0.78) -> Any | None:
    if f.kind in (FieldKind.select,):
        return best_option(value, f.options, cutoff=cutoff)
    if f.kind == FieldKind.multiselect:
        vals = value if isinstance(value, list) else [value]
        out = [o for o in (best_option(v, f.options, cutoff=cutoff) for v in vals) if o]
        return out or None
    if f.kind == FieldKind.boolean:
        nv = normalize(str(value))
        return True if nv in _YES else False if nv in _NO else (value if isinstance(value, bool) else None)
    if f.kind in (FieldKind.text, FieldKind.textarea, FieldKind.location, FieldKind.date, FieldKind.number,
                  FieldKind.unknown):
        if f.options:  # unknown kind that actually has options
            return best_option(value, f.options, cutoff=cutoff)
        s = str(value)
        return s[: f.max_length] if f.max_length and len(s) > f.max_length else s
    return None


@dataclass
class MapResult:
    answers: list[Answer] = field(default_factory=list)
    unresolved: list[FormField] = field(default_factory=list)
    omitted: list[FormField] = field(default_factory=list)   # derived fields with no source (never tasks)


def map_fields(fields: list[FormField], profile: ProfileLike, *, referral_preference: list[str] | None = None,
               min_score: float = 0.78) -> MapResult:
    pref = DEFAULT_REFERRAL_PREFERENCE if referral_preference is None else referral_preference
    res = MapResult()

    def ok(f: FormField, v: Any, src: str) -> None:
        if v is None or v == "":
            res.unresolved.append(f)
        else:
            res.answers.append(Answer(key=f.key, value=v, source=src))

    for f in fields:
        if f.kind == FieldKind.file:
            path = _file_for(f.label, profile)
            if path:
                res.answers.append(Answer(key=f.key, value=path, source="profile"))
            elif f.required:
                res.unresolved.append(f)
            continue
        g = geo_kind(f)
        if g:  # derived from a configured location; otherwise omitted, never a client task
            v = profile.lookup(g)
            if v in (None, ""):
                res.omitted.append(f)
            else:
                res.answers.append(Answer(key=f.key, value=str(v), source="derived:geo"))
            continue
        ck = canonical_key(f.label)
        if ck == "graduation" and f.kind not in (FieldKind.boolean,):
            ok(f, derive_graduation(f, profile.lookup("graduation")), "derived:graduation")
            continue
        if ck in DEMOGRAPHIC_KEYS or (ck is None and _DEMOGRAPHIC_LABEL.search(f.label)):
            v = profile.lookup(ck) if ck else None  # profile.demographics only, never free-form answers
            if v is None or v == "":
                res.unresolved.append(f)
            elif not f.options:
                ok(f, str(v), f"rule:{ck}")
            else:
                dv = str(v)
                if normalize(dv) in ("decline", "prefer not to say") or normalize(dv).startswith("decline"):
                    got = decline_option(f.options)
                else:
                    got = exact_option(dv, f.options) or decline_option(f.options)
                ok(f, got, f"rule:{ck}")
            continue
        if ck == "referral_source" and f.options:
            ok(f, pick_source(profile.lookup("referral_source"), f.options, pref), "rule:referral_source")
            continue
        value: Any | None = profile.lookup(ck) if ck else None
        if value is None:
            value = profile.lookup(normalize(f.label))
        coerced = _coerce(f, value, min_score) if value is not None else None
        if coerced is None or coerced == "":
            res.unresolved.append(f)
        else:
            res.answers.append(Answer(key=f.key, value=coerced, source="profile" if ck is None else f"rule:{ck}"))
    return res

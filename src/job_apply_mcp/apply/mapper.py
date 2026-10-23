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
from collections.abc import Sequence
from dataclasses import dataclass, field
from typing import Any

from ..core.models import Answer, FieldKind, FormField
from .derive import pick_languages, whole_years, years_option
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
    # ADR-0030 semantic keys first: "require relocation support" is NOT "willing to relocate"
    ("relocation_support", (r"relocation (support|assistance|package|bonus|help|benefits?|allowance)",
                            r"\b(require|need)\b.*\brelocation\b")),
    ("knows_employee", (r"\bknow (anyone|someone|any ?one|a current|an? (employee|person|member))",
                        r"\b(were|are|was) you referred\b", r"referred by (an? |a current )?(employee|team)",
                        r"\bany ?one\b.*\b(works?|working) (at|for|here)\b")),
    ("years_experience", (r"\b(years?|yrs?)\b.*\bexperience\b", r"\bexperience\b.*\b(years?|yrs?)\b",
                          r"\bhow many (total )?(years|yrs)\b")),
    ("languages_known", (r"\blanguages?\b.*\b(proficient|production|programming|coding|comfortable|strongest|primary|expert)",
                        r"\b(proficient|programming|coding)\b.*\blanguages?\b",
                        r"\b(strongest|primary|main|favou?rite|preferred|best)\b.*\b(coding|programming|code)\b.*\blanguage\b")),
    # ADR-0040: "country/countries of citizenship" (export control). Before us_citizen so "... (US citizens: write USA)"
    # takes the strict vocabulary check below instead of a Yes/No citizen answer.
    ("citizenship_countries", (r"\bcountr(y|ies)( s)? of (your )?citizenships?\b", r"\bcitizenship countr(y|ies)\b",
                               r"\b(which|what) countr(y|ies)\b.*\bcitizen", r"\bcountr(y|ies)\b.*\b(are you|you are) a citizen\b")),
    ("us_citizen", (r"\b(u s|us|usa|united states|american) citizen",)),
    # ADR-0033 keys
    ("citizenship", (r"^(your |current )?citizenship( status)?$", r"\bcitizenship status\b",
                     r"^what is your (current )?citizenship( status)?$")),
    ("gpa", (r"\bgpa\b", r"grade point average")),
    ("prior_employee", (r"\b(previously|ever|formerly|before)\b.*\b(employed|worked|interned)\b.*\b(at|for|by|with)\b",
                        r"\b(current|former|previous|past|prior) (employee|contractor|intern)\b",
                        r"\bhave you (ever )?(been employed|worked) (at|by|for)\b")),
    ("marketing_optin", (r"\btalent (community|network|pool)\b", r"\bjoin (our|the|\w+ s) (talent|community|network)\b",
                         r"\b(newsletters?|marketing (emails?|communications?|messages?))\b")),
    ("first_name", (r"^(legal )?first name$", r"^given name$")),
    ("last_name", (r"^(legal )?(last|family) name$", r"^surname$")),
    ("full_name", (r"^(your )?(full |legal |full legal |legal full )?name$",)),
    ("preferred_name", (r"^preferred (first )?name$",)),
    ("email", (r"^e ?mail( address)?$",)),
    ("phone", (r"^(mobile |cell )?phone( number)?$", r"^mobile( number)?$")),
    ("linkedin", (r"linkedin",)),
    ("github", (r"github",)),
    ("website", (r"^(personal )?(website|portfolio)( url)?$", r"^website$", r"^portfolio")),
    ("location", (r"^(your )?(current )?location( city)?$", r"^city$", r"^current city$",
                  r"^where are you (currently )?(located|based|living|residing)$",
                  r"^where do you (currently )?(live|reside)$", r"^(your )?(current )?(city|location) of residence$")),
    ("country", (r"^country( of residence)?$",)),
    ("current_company", (r"^current (company|employer)$",)),
    ("current_title", (r"^current (job )?title$",)),
    ("work_authorized", (r"authorized to work", r"legally authorized", r"eligible to work", r"work authorization",
                         r"authori[sz]ation to work", r"(permitted|allowed) to work", r"right to work")),
    ("requires_sponsorship", (r"sponsorship", r"require.* visa")),
    ("willing_to_relocate", (r"relocat",)),
    ("open_to_hybrid", (r"\bhybrid\b",)),
    ("open_to_onsite", (r"\b(open|willing|able|comfortable|commit)\b.*\b(on site|onsite|in office|in person)\b",)),
    ("located_in_us", ((r"(located|based|reside|live|residing)\b.*\b(in|within) the ((continental|contiguous) )?"
                        r"(us|u s|usa|united states)\b"),)),
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
    ("graduation", (r"graduat", r"\byear\b.*\b(complete|completed|finish|earn|earned|receive|received|obtain)\b.*\bdegree",
                    r"\bdegree\b.*\b(completion|year)\b")),
    ("zip", (r"^(zip|postal)( code)?$",)),
    ("over_18", (r"18 years|at least 18|legal age",)),
    ("privacy_ack", (r"privacy (policy|notice)", r"acknowledge",)),
]

DECLINE_MARKERS = ("decline", "prefer not", "don t wish", "do not wish", "do not want", "not to answer",
                   "rather not", "choose not", "not to disclose", "no answer")
_YES = {"yes", "true", "y", "1"}
_NO = {"no", "false", "n", "0"}


def canonical_key(label: str, skip: tuple[str, ...] = ()) -> str | None:
    n = normalize(label)
    for key, pats in ALIASES:
        if key not in skip and any(re.search(p, n) for p in pats):
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


# Intent layer (ADR-0011 Amendment 2): willingness vocabulary. "am"/"do"/"have" are NOT willingness; an option
# like "I am local to the area" is neutral, so a Yes cannot land on it.
INTENT_AFFIRM = {"yes", "willing", "open", "able", "happy", "ready", "eager", "excited", "comfortable", "can",
                 "will", "authorized", "eligible", "true", "relocate", "relocating", "relocation", "agree"}


def option_intent(text: str) -> tuple[int, int]:
    """(-1|0|1, strength): negators -> negative; else count of distinct willingness words (0 -> neutral)."""
    toks = tokens(text)
    if re.search(r"\bno (?:preference|difference)\b|\beither\b|\bboth\b|\bany\b", " ".join(toks)):
        return 0, 0
    if any(w in _NEGATORS for w in toks):
        return -1, 0
    hits = len(set(toks) & INTENT_AFFIRM)
    return (1, hits) if hits else (0, 0)


def _yes_no_option(value_yes: bool, options: list[str]) -> str | None:
    """Map Yes -> the most specific affirmative option, No -> the negative option; neutral/ambiguous -> None."""
    scored = [(o, *option_intent(o)) for o in options]
    if value_yes:
        aff = [(o, st) for o, pol, st in scored if pol == 1]
        if not aff:
            return None
        top = max(st for _, st in aff)
        best = [o for o, st in aff if st == top]
        return best[0] if len(best) == 1 else None
    neg = [o for o, pol, _ in scored if pol == -1]
    if len(neg) > 1:
        lead = [o for o in neg if tokens(o)[:1] == ["no"]]
        neg = lead or neg
    return neg[0] if len(neg) == 1 else None


# explicit EEO synonyms (ADR-0040): the only non-identical pairs exact_option accepts
_EEO_SYNONYMS = [frozenset(["man", "male"]), frozenset(["woman", "female"]),
                 frozenset(["non binary", "nonbinary", "non-binary"])]


def exact_option(value: Any, options: list[str]) -> str | None:
    """Near-exact only (equal after normalization, or equal/nested token sets of >=1 word, or an explicit synonym).
    For demographic, pronoun and EEO answers, where a semantic guess is never acceptable."""
    nv = normalize(str(value))
    syn = next((g for g in _EEO_SYNONYMS if nv in g), None)
    if syn:
        hit = [o for o in options if normalize(o) in syn]
        if len(hit) == 1:
            return hit[0]
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
    if option_topics(sv) or any(option_topics(o) for o in options):
        return exact_option(sv, options)   # ADR-0040: demographic values never snap by containment or similarity
    if nv in _YES | _NO:
        return _yes_no_option(nv in _YES, list(nopts.values()))
    vneg = has_negator(sv)
    vpol = polarity(sv)
    ok = {n: o for n, o in nopts.items() if has_negator(o) == vneg}
    if vpol == 1 and not vneg:  # affirmative sentence: never land on a negated option
        ok = {n: o for n, o in ok.items() if polarity(o) >= 0}
    # containment, preferring the tightest (shortest) container; require word-level overlap
    cands = [o for n, o in ok.items()
             if nv and n and (re.search(rf"\b{re.escape(nv)}\b", n) or re.search(rf"\b{re.escape(n)}\b", nv))]
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



# ---- semantic answers (ADR-0030) ------------------------------------------------------------------
_YESNO_STYLE = re.compile(r"^(are|do|does|did|will|would|can|could|is|have|has|if|were)\b|"
                          r"\b(open|willing|able|comfortable|commit|ready)\b")
_SPONS_NEG = re.compile(r"without\b.*\b(sponsor|visa)|\bnot (?:now or in the future )?(?:require|need)\b.*\b(sponsor|visa)|"
                        r"\bno\b.*\b(sponsor|visa)|\b(sponsor|visa)\w*\b.* not (?:required|needed)")
_AUTH_WORDS = re.compile(r"authori[sz]|eligible to work|permitted to work|allowed to work|right to work|legally")
_AUTH_NEG = re.compile(r"\bunauthori[sz]ed\b|\bunable to work\b|\bineligible\b|"
                       r"\bnot (?:legally )?(?:authori[sz]ed|eligible|permitted|allowed)\b")
_SPOKEN = re.compile(r"speak|spoken|fluen|english|native|written|verbal|translat", re.IGNORECASE)
_SEMANTIC_KEYS = {"work_authorized", "requires_sponsorship", "us_citizen", "relocation_support", "open_to_hybrid",
                  "open_to_onsite", "knows_employee", "years_experience", "languages_known", "citizenship", "gpa",
                  "prior_employee", "marketing_optin", "citizenship_countries", "located_in_us"}
# Never sent to the local LLM: a guess is a false statement about the candidate.
SEMANTIC_SENSITIVE = _SEMANTIC_KEYS | {"willing_to_relocate"}
_TOTAL_GENERIC = frozenset(
    ["how", "many", "total", "years", "yrs", "year", "of", "experience", "professional", "software", "engineering", "engineer", "work", "working", "relevant", "practical", "career", "industry", "overall", "do", "you", "have", "at", "least", "a", "an", "the", "your", "please", "input", "enter", "number", "in", "that", "are", "to", "this", "role", "position", "related", "full", "time", "hands", "on", "is", "what", "minimum", "prior", "paid", "commercial", "real", "world", "combined", "cumulative", "amount", "can", "say", "for", "with", "or", "and", "i", "am", "if", "any", "has", "s", "plus", "more", "field"]
)


def _truth(v: Any) -> bool | None:
    if isinstance(v, bool):
        return v
    t = tokens(str(v))[:1]
    return True if t == ["yes"] else False if t == ["no"] else None


def _yn(b: bool | None) -> str | None:
    return None if b is None else "Yes" if b else "No"


def _yes_no_options(options: list[str]) -> bool:
    return any(tokens(o)[:1] in (["yes"], ["no"]) for o in options)


# "...valid work permit and do not need a company to sponsor my visa": the option that needs NO sponsorship
_NO_SPONSOR = re.compile(r"without\b.*\b(sponsor|visa)|\b(do|does|will) not (?:\w+ ){0,2}(require|need)\b|\bno (?:\w+ ){0,2}sponsor|"
                         r"\bnot (?:\w+ ){0,2}(require|need)\b|\bnon[- ]?sponsor")


def pick_auth_option(options: list[str], authorized: bool | None, sponsorship: bool | None,
                     citizen: bool | None) -> str | None:
    """Non yes/no authorization choices ("authorization to work in the country where you live"): the one option
    consistent with the profile facts, else None. Citizen/permanent-resident options need citizen=True."""
    if authorized is not True:
        return None
    pool = []
    for o in options:
        lo = o.lower()
        if re.search(r"citizen|national\b|nationality|permanent resident|green card|settled|indefinite", lo) and citizen is not True:
            continue
        if re.search(r"\b(not|no|don't|do not)\b.{0,25}\b(right|authori[sz]ed|permit|permission|eligible|entitled)\b|unauthori[sz]ed", lo):
            continue
        pool.append(o)
    if sponsorship is True:
        pool = [o for o in pool if re.search(r"sponsor|visa|permit|permission|require|work authori[sz]ation", o.lower())
                and not _NO_SPONSOR.search(o.lower())]
        if len(pool) > 1:
            pool = [o for o in pool if re.search(r"sponsor", o.lower()) and re.search(r"authori[sz]|right|eligible|permit|permission", o.lower())] or pool
    elif sponsorship is False:
        pool = [o for o in pool if re.search(r"authori[sz]|right to work|eligible|permit|permission", o.lower())
                and (not re.search(r"sponsor|visa|require", o.lower()) or _NO_SPONSOR.search(o.lower()))]
    else:
        return None
    return pool[0] if len(pool) == 1 else None


_US_NAMES = frozenset(["us", "usa", "unitedstates", "unitedstatesofamerica", "america"])


def is_us_country(value: str) -> bool:
    return re.sub(r"[^a-z]", "", str(value).lower()) in _US_NAMES


def pick_citizenship_option(options: list[str], citizen: bool | None, authorized: bool | None) -> str | None:
    """"Citizenship Status" choices. Citizen False -> the single explicit non-citizen option, else the single
    visa-holder option (needs authorized=True). Never a citizen, permanent-resident, refugee, asylee or DACA option.
    Citizen True -> the single plain citizen option. Anything ambiguous or inconsistent -> None."""
    if citizen is None:
        return None
    pr = re.compile(r"permanent resident|green card|lawful permanent|refugee|asylee|daca|deferred action|settled")
    non = re.compile(r"\bnon[- ]?(?:u\.?s\.?|us|american)?[- ]?citizen|\bnot a (?:u\.?s\.? |american )?citizen|foreign national|"
                     r"citizen of (?:another|a different|an?other|a non|other)|international")
    visa = re.compile(r"\bvisa\b|\bh-?1b|\bf-?1\b|\bopt\b|\bcpt\b|work permit|\btn\b|\bl-?1\b|\bj-?1\b|\be-?3\b")
    cand = [(o, o.lower()) for o in options if not re.search(r"\bother\b|prefer not|decline", o.lower())]
    if citizen is True:
        hit = [o for o, lo in cand if re.search(r"citizen|national of", lo) and not non.search(lo) and not pr.search(lo)]
        return hit[0] if len(hit) == 1 else None
    explicit = [o for o, lo in cand if non.search(lo) and not pr.search(lo)]
    if len(explicit) == 1:
        return explicit[0]
    if explicit or authorized is not True:
        return None
    vis = [o for o, lo in cand if visa.search(lo) and not pr.search(lo) and not re.search(r"citizen|national of", lo)]
    return vis[0] if len(vis) == 1 else None


_NUM = re.compile(r"\d+(?:\.\d+)?")


def gpa_number(value: Any) -> float | None:
    """Numeric GPA; a value on an explicit non-4.0 scale ("8.5/10") is not comparable -> None."""
    s = str(value or "").strip()
    m = re.fullmatch(r"(\d+(?:\.\d+)?)\s*(?:/\s*(\d+(?:\.\d+)?))?", s)
    if not m:
        return None
    if m.group(2) and float(m.group(2)) != 4.0:
        return None
    return float(m.group(1))


def pick_gpa_option(value: float, options: list[str]) -> str | None:
    """Exact value, or the single range option that contains it ("3.5 - 3.69", "Below 3.0"). Never rounds."""
    hits = []
    for o in options:
        lo_t = o.lower()
        nums = [float(x) for x in _NUM.findall(o)]
        if not nums or re.search(r"outside|not applicable|prefer", lo_t):
            continue
        if len(nums) >= 2 and re.search(r"\d\s*(?:-|\u2013|\u2014|to)\s*\d", lo_t):
            ok = nums[0] <= value <= nums[1]
        elif len(nums) == 1 and re.search(r"below|under|less than|<", lo_t):
            ok = value < nums[0]
        elif len(nums) == 1 and re.search(r"above|over|at least|more than|\+|>", lo_t):
            ok = value >= nums[0]
        elif len(nums) == 1:
            ok = abs(value - nums[0]) < 1e-9
        else:
            ok = False
        if ok:
            hits.append(o)
    return hits[0] if len(hits) == 1 else None


def _gpa_level(label: str) -> str:
    n = normalize(label)
    if re.search(r"undergrad|bachelor", n):
        return "undergrad"
    if re.search(r"doctor|phd", n):
        return "doctorate"
    if re.search(r"graduate|master|postgrad", n):
        return "graduate"
    return "any"


_CONSENT = re.compile(r"consent|agree|acknowledg|privacy|gdpr|attest|certif|authori[sz]e", re.IGNORECASE)


# ---- country of an authorization / sponsorship question (ADR-0038) ---------------------------------
# The structured [authorization] facts describe the United States only. A question naming another country is answered
# only from the user's own [answers] pattern that names that country; otherwise it stays unresolved.
_US_IN_LABEL = re.compile(r"\b(u s a?|usa|united states( of america)?|america|american)\b")
_COUNTRY_PATTERNS: dict[str, str] = {
    "united kingdom": r"united kingdom|\buk\b|\bu k\b|great britain|\bbritain\b|\bbritish\b|england|scotland|wales|northern ireland",
    "canada": r"canada|canadian", "ireland": r"(?<!northern )ireland|\birish\b",
    "european union": r"european union|\beu\b|\beea\b|\beurope\b|schengen",
    "germany": r"germany|\bgerman\b", "france": r"france|french", "netherlands": r"netherlands|holland|dutch",
    "spain": r"\bspain\b", "italy": r"\bitaly\b", "portugal": r"portugal", "poland": r"poland", "sweden": r"sweden",
    "norway": r"norway", "denmark": r"denmark", "finland": r"finland", "switzerland": r"switzerland|swiss",
    "austria": r"austria", "belgium": r"belgium", "luxembourg": r"luxembourg", "czechia": r"czech",
    "romania": r"romania", "ukraine": r"ukraine", "greece": r"greece", "hungary": r"hungary", "estonia": r"estonia",
    "turkey": r"t(u|ü)rkiye|\bturkey\b", "israel": r"israel", "united arab emirates": r"united arab emirates|\buae\b",
    "india": r"\bindia\b", "pakistan": r"pakistan", "china": r"\bchina\b|\bprc\b", "hong kong": r"hong kong",
    "taiwan": r"taiwan", "japan": r"japan", "south korea": r"korea", "singapore": r"singapore",
    "philippines": r"philippines", "vietnam": r"vietnam", "indonesia": r"indonesia", "malaysia": r"malaysia",
    "thailand": r"thailand", "australia": r"australia", "new zealand": r"new zealand", "mexico": r"(?<!new )mexico",
    "brazil": r"brazil", "argentina": r"argentina", "colombia": r"colombia", "chile": r"\bchile\b", "peru": r"\bperu\b",
    "costa rica": r"costa rica", "south africa": r"south africa", "nigeria": r"nigeria", "kenya": r"kenya",
    "egypt": r"egypt",
}
_COUNTRY_RES = {c: re.compile(p) for c, p in _COUNTRY_PATTERNS.items()}


def question_countries(label: str) -> tuple[bool, list[str]]:
    """(mentions the US, non-US countries named) for an authorization/sponsorship label."""
    n = normalize(label)
    return bool(_US_IN_LABEL.search(n)), [c for c, rx in _COUNTRY_RES.items() if rx.search(n)]


# Deny-by-default (ADR-0038): US facts answer only a label whose every word is generic authorization/sponsorship
# wording or names the US. Any other word (a city, region, unlisted country, "here", "the country where ...") -> the
# question may be about another place, so it stays unresolved.
_AUTH_VOCAB = frozenset(["a", "an", "the", "and", "or", "any", "to", "in", "of", "for", "with", "without", "by", "on", "at", "as", "is", "are", "be", "been", "being", "do", "does", "did", "will", "would", "can", "could", "may", "must", "shall", "should", "if", "not", "no", "now", "currently", "presently", "future", "ever", "you", "your", "yourself", "i", "am", "have", "has", "had", "who", "that", "this", "legally", "lawfully", "legal", "authorized", "authorised", "authorization", "authorisation", "authorize", "unauthorized", "eligible", "eligibility", "permitted", "allowed", "able", "right", "work", "working", "works", "employment", "employed", "employer", "employers", "job", "position", "role", "require", "requires", "required", "requiring", "need", "needs", "needed", "sponsor", "sponsors", "sponsored", "sponsorship", "sponsoring", "visa", "visas", "status", "immigration", "immigrant", "petition", "support", "assistance", "obtain", "maintain", "continue", "continued", "extension", "transfer", "valid", "permit", "current", "h", "1b", "h1b", "l", "1", "l1", "e", "3", "e3", "tn", "opt", "cpt", "stem", "ead", "f", "f1", "j", "j1", "o", "o1", "based", "restriction", "restrictions", "restricted", "unrestricted", "basis", "full", "time", "hired", "offered", "offer", "company", "please", "select", "answer", "yes", "no", "question", "choose", "option", "options", "describes", "describe", "best", "which", "what", "following", "one", "us", "u", "s", "usa", "united", "states", "america", "american"])


def auth_place_ok(label: str) -> bool:
    """True when the label names no place, or only the US (US, U.S., USA, United States, America)."""
    return all(t in _AUTH_VOCAB or t.isdigit() for t in normalize(label).split())


# ADR-0040: the allow-list runs on the QUESTION, after two structural reductions:
#  1. an example clause ("(e.g., H-1B, TN, etc.)", "(i.e. OPT)", "such as F-1 or L-1") is dropped when it lists only US
#     visa types, so "e.g." can no longer make a US sponsorship question look like another place;
#  2. text after the question mark is a note: allowed only when it is authorization/visa/export wording with no place.
# With the posting located only in the US, "the country where the job is located" / "this country" / "this location"
# refer to the US. Everything else still has to pass auth_place_ok.
_EXAMPLE_CLAUSE = re.compile(
    r"\(\s*(?:e\.?\s?g\.?|i\.?\s?e\.?|ex\.?|such as|like|for example|including)\s*[,:]?\s*([^()]*)\)|"
    r"[,;:]?\s*\b(?:e\.\s?g\.|i\.\s?e\.|such as|for example|including)\s*[,:]?\s*([^.?;()]*)", re.IGNORECASE)
_VISA_TYPES = frozenset(["h1b", "1b", "h4", "opt", "cpt", "tn", "f1", "l1", "o1", "e3", "j1", "stem", "ead"])
_VISA_EXAMPLE_VOCAB = _VISA_TYPES | frozenset(["h", "4", "f", "1", "l", "o", "e", "3", "j", "visa", "visas", "status",
                                               "etc", "and", "or", "a", "an", "the", "holder", "holders", "permit",
                                               "work", "sponsorship", "s"])
_AUTH_NOTE_VOCAB = frozenset(["if", "qualify", "qualifies", "defined", "define", "regulations", "note", "considered",
                              "applicants", "candidates", "those", "under", "per", "etc", "e", "g"])
_JOB_PLACE = re.compile(r"\b(?:the|this) country (?:where|in which) (?:the|this) (?:job|role|position) is "
                        r"(?:located|based)\b|\bthis country\b|\bthis location\b|\bthe country of employment\b",
                        re.IGNORECASE)


def _drop_examples(text: str) -> str:
    def sub(m: re.Match[str]) -> str:
        inner = normalize(m.group(1) if m.group(1) is not None else m.group(2) or "").split()
        visa = any(t in _VISA_TYPES for t in inner) or "h" in inner
        return " " if inner and visa and all(t in _VISA_EXAMPLE_VOCAB or t.isdigit() for t in inner) else m.group(0)
    return _EXAMPLE_CLAUSE.sub(sub, text)


def auth_core(label: str, job_in_us: bool = False) -> str | None:
    """The US authorization/sponsorship question inside `label`, or None when it may be about another place."""
    text = _drop_examples(label)
    if job_in_us:
        text = _JOB_PLACE.sub("the United States", text)
    q, _, note = text.partition("?")
    if note.strip():
        words = normalize(note).split()
        if question_countries(note)[1] or not all(t in _AUTH_VOCAB or t in _AUTH_NOTE_VOCAB or t.isdigit()
                                                  for t in words):
            return None
    return q if auth_place_ok(q) else None


# ADR-0040 (review): deny-by-default US posting location. A "City, ST" code counts on its own only when it collides with
# no ISO-3166 country code and no common subdivision code elsewhere; an ambiguous code needs a city from a small explicit
# US city table. Collisions: ISO countries (AL AR AZ CA CO DE GA ID IL IN KY LA MA MD ME MN MO MS MT NC NE PA SC SD TN
# VA), Australia (WA), India (CT, OR, UT, plus AR GA MN TN), Italian provinces (CT, MI, OR, RI, VT, plus AL CO ME MO PA),
# Brazil (AL MA MS MT PA SC). Canadian province codes (AB BC MB NB NL NS NT NU ON PE QC SK YT) share no US code.
_US_STATE_CODES_UNAMBIGUOUS = frozenset(["ak", "fl", "hi", "ia", "ks", "nv", "nh", "nj", "nm", "ny", "nd", "oh", "ok",
                                         "tx", "wv", "wi", "wy", "dc"])
_US_CITY_STATE = frozenset([("san francisco", "ca"), ("los angeles", "ca"), ("san diego", "ca"), ("san jose", "ca"),
                            ("palo alto", "ca"), ("mountain view", "ca"), ("sunnyvale", "ca"), ("long beach", "ca"),
                            ("chicago", "il"), ("boston", "ma"), ("atlanta", "ga"), ("denver", "co"),
                            ("boulder", "co"), ("philadelphia", "pa"), ("pittsburgh", "pa"), ("baltimore", "md"),
                            ("indianapolis", "in"), ("new orleans", "la"), ("minneapolis", "mn"), ("arlington", "va"),
                            ("phoenix", "az"), ("nashville", "tn"), ("charlotte", "nc"), ("raleigh", "nc"),
                            ("louisville", "ky"), ("st louis", "mo"), ("boise", "id"), ("seattle", "wa"),
                            ("redmond", "wa"), ("bellevue", "wa"), ("spokane", "wa"), ("tacoma", "wa"),
                            ("hartford", "ct"), ("stamford", "ct"), ("new haven", "ct"), ("detroit", "mi"),
                            ("ann arbor", "mi"), ("grand rapids", "mi"), ("portland", "or"), ("eugene", "or"),
                            ("salt lake city", "ut"), ("provo", "ut"), ("lehi", "ut"), ("burlington", "vt"),
                            ("providence", "ri")])
# well-known non-US cities that share a name with US places: only an explicit US marker makes them US
_SHARED_CITY_NAMES = frozenset(["perth", "melbourne", "sydney", "london", "paris", "birmingham", "cambridge",
                                "hamilton", "victoria", "vancouver", "kingston"])
# full state names; "georgia" is also a country, so it is left out
_US_STATE_NAMES = re.compile(
    r"\b(?:alabama|alaska|arizona|arkansas|california|colorado|connecticut|delaware|florida|hawaii|idaho|illinois|"
    r"indiana|iowa|kansas|kentucky|louisiana|maine|maryland|massachusetts|michigan|minnesota|mississippi|missouri|"
    r"montana|nebraska|nevada|new hampshire|new jersey|new mexico|new york|north carolina|north dakota|ohio|oklahoma|"
    r"oregon|pennsylvania|rhode island|south carolina|south dakota|tennessee|texas|utah|vermont|virginia|"
    r"washington|west virginia|wisconsin|wyoming)\b")
_US_EXPLICIT = re.compile(r"\b(?:united states(?: of america)?|usa|u s a?|us)\b")


def _part_is_us(part: str) -> bool:
    """Only the LAST comma component decides: an explicit US marker, a full US state name, or "City, ST"."""
    if question_countries(part)[1]:
        return False
    comps = [normalize(c) for c in part.split(",") if normalize(c)]
    if not comps:
        return False
    tail = comps[-1]
    if _US_EXPLICIT.search(tail) or _US_STATE_NAMES.fullmatch(tail):
        return True
    if len(comps) < 2 or not re.fullmatch(r"[a-z]{2}", tail):
        return False
    city, code = comps[-2], tail
    if city in _SHARED_CITY_NAMES:
        return False
    return code in _US_STATE_CODES_UNAMBIGUOUS or (city, code) in _US_CITY_STATE


def _parenthetical(m: re.Match[str]) -> str:
    """"(US)" / "(USA)" / "(United States)" become a trailing component; any other parenthetical ("(HQ)") is dropped."""
    return f", {m.group(1)}" if _US_EXPLICIT.fullmatch(normalize(m.group(1))) else " "


def location_is_us(location: str) -> bool:
    """True only when every part of a posting location is explicitly a US place ("New York, NY", "Remote - US",
    "Remote (US)", "Austin, Texas"). No location, "Remote", a non-US country or an ambiguous code alone -> False."""
    text = re.sub(r"\(([^)]*)\)", _parenthetical, location or "")
    parts = [p for p in re.split(r"[;|]|\s/\s|\bor\b", text) if p.strip()]
    return bool(parts) and all(_part_is_us(p) for p in parts)


_MULTI_REGION = re.compile(r"\b(?:americas?|north america|latam|latin america|emea|apac|asia|europe|global|worldwide|"
                           r"international|anywhere|multiple|several|various|or)\b|[;/|&]", re.IGNORECASE)


def job_is_us(location: str, countries: Sequence[str] = (), locations: Sequence[str] = ()) -> bool:
    """ONE conjunctive, deny-by-default rule (ADR-0040). True only if
    (a) the structured adapter countries are exactly ["US"] (every location entry parsed; a malformed or country-less
        entry is "UNCONFIRMED"), AND
    (b) every available location string (posting location, offices, allLocations, secondaryLocations) is a US place by
        `location_is_us`, AND
    (c) no location string names a region or several places (americas, latam, emea, apac, europe, global, worldwide,
        international, anywhere, multiple, "or", ";", "/", ...).
    No text fallback: without a structured country the job is not confirmed, and text never overrides structure."""
    if list(dict.fromkeys(countries)) != ["US"]:
        return False
    texts = [t for t in (location, *locations) if str(t or "").strip()]
    return all(not _MULTI_REGION.search(str(t)) and location_is_us(str(t)) for t in texts)


# ADR-0040 (control review): an authorization/sponsorship rule never answers a field whose label, help text or options
# mention export control or a legal status other than work authorization. "Authorized to work in the US? (U.S. persons
# as defined under ITAR/EAR)" is an export-control question; it stays with the owner (and never goes to an LLM).
_AUTH_LEGAL_BLOCK = re.compile(r"\bitar\b|\bear\b|\bu ?s persons?\b|\bexport|\bcitizen\w*|permanent resident\w*|"
                               r"green card|\bclearance\b|deemed export")


def auth_legal_blocked(f: FormField) -> bool:
    return any(_AUTH_LEGAL_BLOCK.search(normalize(t)) for t in (f.label, *f.options))


# The US named as the PLACE of the work: "in the US", "within the United States", "in the U.S.A.". Visa examples
# ("e.g., H-1B") and "work for US" (the company) never count; on an unconfirmed job they leave the field unresolved.
_US_PLACE = re.compile(r"\b(?:in|within|inside) (?:the )?(?:U\.?S\.?A?\.?(?![A-Za-z])|united states(?: of america)?\b|"
                       r"america\b)", re.IGNORECASE)


def names_us(text: str) -> bool:
    """The text names the United States as a place ("... to work in the US?"), after example clauses are dropped."""
    t = _drop_examples(text or "")
    for m in _US_PLACE.finditer(t):
        hit = m.group(0)
        if re.search(r"united states|america", hit, re.IGNORECASE) or re.search(r"U\.?S", hit):   # "US" in capitals only
            return True
    return False


def _auth_answer(f: FormField, ck: str, profile: ProfileLike, job_in_us: bool = False) -> tuple[bool, Any]:
    if auth_legal_blocked(f):
        return True, None
    core = auth_core(f.label, job_in_us)
    # ADR-0040: the [authorization] facts are US facts. Unless the job is confirmed in the US, only a question that
    # names the US itself is answered; "Will you require sponsorship?" for a Perth/Toronto/Remote job stays unresolved.
    if core is not None and not job_in_us and not names_us(core):
        return True, None
    n = normalize(core if core is not None else f.label)
    if core is None:
        us, foreign = question_countries(f.label)
        if len(foreign) == 1 and not us:     # only the user's own [answers] pattern naming that country
            return True, profile.lookup(f"country_answer:{foreign[0]}:{n}")
        return True, None
    auth, req = _truth(profile.lookup("work_authorized")), _truth(profile.lookup("requires_sponsorship"))
    cit = _truth(profile.lookup("us_citizen"))
    ma, ms = _AUTH_WORDS.search(n), re.search(r"sponsor|\bvisa\b", n)
    has_auth, has_sp = bool(ma), bool(ms)
    if ma and ms and ms.start() < ma.start():    # "require sponsorship ... for work authorization": a sponsorship question
        has_auth = False
    if f.options and not _yes_no_options(f.options):
        if has_auth:
            return True, pick_auth_option(f.options, auth, req, cit)
        return False, None
    if has_auth and has_sp:                  # compound: only the explicit "authorized ... without sponsorship" form
        if _SPONS_NEG.search(n) and auth is not None and req is not None:
            return True, _yn(auth and not req)
        return True, None
    if ck == "requires_sponsorship" or (has_sp and not has_auth):
        if req is None:
            return False, None
        return True, _yn(not req if _SPONS_NEG.search(n) else req)
    if auth is None:
        return False, None
    return True, _yn(not auth if _AUTH_NEG.search(n) else auth)


def _tech_years(label: str, skills: dict[str, float]) -> float | None:
    low = label.lower()
    hits = [y for name, y in skills.items()
            if re.search(rf"(?<![\w+#.]){re.escape(name)}(?![\w+#]|\.\w)", low)]
    return max(hits) if hits else None


def _years_answer(f: FormField, profile: ProfileLike) -> Any | None:
    """Total years from the experience entries, or per-technology years only when the profile states them.
    "At least N years" -> Yes/No by comparison. Never rounded up, never guessed."""
    n = normalize(f.label)
    tech = _tech_years(f.label, profile.lookup("skill_years") or {})
    if tech is not None:
        years = tech
    elif not (set(tokens(n)) - _TOTAL_GENERIC - {w for w in tokens(n) if w.isdigit()}):
        raw = profile.lookup("years_experience")
        years = float(raw) if raw not in (None, "") else None
    else:
        return None
    if years is None:
        return None
    m = re.search(r"\b(\d+)\s*\+?\s*(?:or more\s+)?(?:years?|yrs?)\b", n)
    if m and _YESNO_STYLE.search(n):
        return "Yes" if years >= int(m.group(1)) else "No"
    if f.options:
        return years_option(years, f.options)
    if f.kind in (FieldKind.number, FieldKind.text, FieldKind.unknown):
        return str(whole_years(years))
    return None


# ---- prior-employee scope (ADR-0038) --------------------------------------------------------------
# Deny-by-default: answered only for a direct-employment question about the posting company itself (or a configured
# alias, or "this company"/"us"). The company name is replaced by a placeholder and the whole label must then match
# one of the _DIRECT_EMPLOY forms, with nothing left over except tense/time words. A company whose own name contains
# a qualifier word ("State Street", "Agency Inc", "Competitor Labs") is never answered: the label is ambiguous.
_CO = "zzcozz"
_SELF_REF = re.compile(r"\b(?:this|the) (?:company|organi[sz]ation)\b|(?<=\bfor )us\b|(?<=\bby )us\b|(?<=\bat )us\b|"
                       r"(?<=\bwith )us\b")
_TIME = r"(?:(?:ever|previously|formerly|currently|before|once) )*"
_DIRECT_EMPLOY = re.compile(
    rf"^(?:have|did|do) you {_TIME}(?:been employed|worked|work|been an? (?:employee|intern)) {_TIME}(?:by|for|at|with) {_CO}"
    rf"(?: {_TIME}| before| previously| for any (?:length|period) of time)?$|"
    rf"^(?:are|were) you {_TIME}(?:been )?employed (?:by|at) {_CO}(?: before| previously| for any (?:length|period) of time)?$|"
    rf"^(?:are|were) you an? (?:current or former|former or current|current|former|previous|past|prior) "
    rf"(?:employee|intern) (?:of|at) {_CO}$")
_CO_QUALIFIERS = frozenset(["state", "states", "federal", "government", "agency", "competitor", "competitors",
                            "affiliate", "affiliates", "parent", "subsidiary", "subsidiaries", "partner", "partners",
                            "client", "clients", "vendor", "vendors", "public", "national", "department",
                            # public-sector employers (ADR-0040): "worked for the county?" is not about "County Labs"
                            "county", "counties", "city", "cities", "municipal", "municipality", "police", "township",
                            "borough", "sheriff", "university", "universities", "usaf"])
# military / defence / public-body phrases: "worked for Army?" must never be read as the posting company "Army Labs"
_CO_QUALIFIER_PHRASES = re.compile(r"\b(army|navy|air force|armed forces|marines?|military|defen[cs]e|coast guard|"
                                   r"national guard|veterans?|transit authority|school district)\b")
_GENERIC_CO_WORDS = frozenset(["the", "global", "united", "american", "national", "general", "first", "international",
                               "data", "systems", "group", "technologies", "digital", "new", "world", "health"])


def _mark_company(words: list[str], name: str) -> tuple[list[str], bool]:
    from .adapters import _co_key
    key = _co_key(name)
    if not key:
        return words, False
    for size in range(min(6, len(words)), 0, -1):
        for i in range(len(words) - size + 1):
            if _CO not in words[i:i + size] and _co_key(" ".join(words[i:i + size])) == key:
                return words[:i] + [_CO] + words[i + size:], True
    lead = [t for t in normalize(name).split() if t not in _GENERIC_CO_WORDS]
    if lead and len(lead[0]) >= 4 and lead[0] in words:
        i = words.index(lead[0])
        return words[:i] + [_CO] + words[i + 1:], True
    return words, False


def about_this_company(label: str, company: str, aliases: Sequence[str] = ()) -> bool:
    """True only for a direct-employment question about the posting company itself (or a configured alias)."""
    names = [x for x in (company, *aliases) if normalize(x)]
    if not names or any(t in _CO_QUALIFIERS for x in names for t in normalize(x).split()) \
            or any(_CO_QUALIFIER_PHRASES.search(normalize(x)) for x in names):
        return False
    words = _SELF_REF.sub(_CO, normalize(label)).split()
    for name in names:
        found = True
        while found:
            words, found = _mark_company(words, name)
    return bool(_DIRECT_EMPLOY.match(" ".join(words)))


# ---- export-control citizenship countries (ADR-0040) ----------------------------------------------
# Deny-by-default: answered from the profile's citizenship fact only when every word of the label asks for the
# country/countries of citizenship. Permanent residence, dates ("when obtained"), "U.S. person", ITAR, dual/former
# citizenship, someone else's citizenship or any other word leave the field unresolved.
_CIT_VOCAB = frozenset(["a", "an", "the", "of", "and", "or", "for", "in", "is", "are", "am", "i", "you", "your", "do",
                        "what", "which", "please", "list", "all", "any", "provide", "enter", "indicate", "select",
                        "name", "names", "state", "current", "currently", "hold", "country", "countries", "s",
                        "citizen", "citizenship", "citizenships", "export", "control", "controls", "compliance",
                        "purposes", "purpose"])


def citizenship_countries_ok(label: str) -> bool:
    """True when the label asks only for the candidate's country/countries of citizenship."""
    words = normalize(label).split()
    return bool(words) and all(t in _CIT_VOCAB for t in words)


def _citizenship_countries(f: FormField, profile: ProfileLike) -> Any | None:
    c = profile.lookup("citizenship_country")
    if not c or not citizenship_countries_ok(f.label):
        return None
    if not f.options:
        return str(c) if f.kind in (FieldKind.text, FieldKind.textarea, FieldKind.unknown) else None
    parts = [x.strip() for x in re.split(r"[,;/]| and ", str(c)) if x.strip()]
    picks = [exact_option(x, f.options) for x in parts]
    if not picks or any(p is None for p in picks):
        return None
    if f.kind == FieldKind.multiselect:
        return list(dict.fromkeys(picks))
    return picks[0] if len(picks) == 1 else None


_RESIDE_VOCAB = frozenset(["do", "you", "are", "currently", "presently", "reside", "live", "living", "residing",
                           "located", "based", "in", "within", "the", "continental", "contiguous", "us", "u", "s",
                           "usa", "united", "states", "of", "america", "your", "is"])
_NONCONTIGUOUS = frozenset(["ak", "alaska", "hi", "hawaii", "pr", "puerto rico", "gu", "guam", "vi", "virgin islands",
                            "as", "american samoa", "mp", "northern mariana islands"])


def _residence_answer(f: FormField, profile: ProfileLike) -> Any | None:
    """ADR-0040: "Do you (currently) reside/live/are you located in the (continental) US?" from the profile location
    only; any other wording, or a continental question without a configured state, stays unresolved."""
    n = normalize(f.label)
    if not all(t in _RESIDE_VOCAB for t in n.split()):
        return None
    v = _truth(profile.lookup("located_in_us"))
    if v is None:
        return None
    if v and re.search(r"\b(continental|contiguous)\b", n):
        region = normalize(str(profile.lookup("derived:region") or ""))
        if not region:
            return None
        if region in _NONCONTIGUOUS:
            return "No"
    return _yn(v)


def _semantic(f: FormField, ck: str, profile: ProfileLike, company: str, top_languages: int,
              company_aliases: Sequence[str] = (), job_in_us: bool = False) -> tuple[bool, Any]:
    """(handled, value). handled=False -> continue with the generic profile lookup."""
    n = normalize(f.label)
    if ck == "located_in_us":
        return True, _residence_answer(f, profile)
    if ck == "us_citizen":
        if _AUTH_WORDS.search(n):
            return _auth_answer(f, "work_authorized", profile, job_in_us)
        if re.search(r"permanent resident|green card|national\b", n):
            return True, None
        v = profile.lookup("us_citizen")
        return (False, None) if v is None else (True, v)
    if ck in ("work_authorized", "requires_sponsorship"):
        return _auth_answer(f, ck, profile, job_in_us)
    if ck in ("relocation_support", "open_to_hybrid", "open_to_onsite"):
        if not _YESNO_STYLE.search(n):
            return True, None
        v = profile.lookup(ck)
        return (False, None) if v is None else (True, v)
    if ck == "knows_employee":
        if not company:
            return True, None
        who = profile.lookup("referral:" + normalize(company))
        if who in (None, ""):
            return True, "No"
        return True, ("Yes" if f.options or f.kind == FieldKind.boolean else str(who))
    if ck == "years_experience":
        return True, _years_answer(f, profile)
    if ck == "citizenship_countries":
        return True, _citizenship_countries(f, profile)
    if ck == "citizenship":
        cit = _truth(profile.lookup("us_citizen"))
        if not f.options:
            c = profile.lookup("citizenship_country")
            return True, (str(c) if c else None)
        return True, pick_citizenship_option(f.options, cit, _truth(profile.lookup("work_authorized")))
    if ck == "gpa":
        raw = profile.lookup("gpa:" + _gpa_level(f.label))
        g = gpa_number(raw)
        if g is None:
            return True, None
        if f.options:
            return True, pick_gpa_option(g, f.options)
        return True, re.sub(r"\s*/.*$", "", str(raw).strip())
    if ck == "prior_employee":
        if not company or not about_this_company(f.label, company, company_aliases):   # ADR-0038: government/competitor/other employers
            return True, profile.lookup(n)                            # only the user's own explicit answer
        return True, "Yes" if profile.lookup("employer:" + normalize(company)) else "No"
    if ck == "marketing_optin":
        if _CONSENT.search(f.label):          # consent/privacy acknowledgements need the owner's decision
            return True, None
        return True, "No"
    if ck == "languages_known":
        if _SPOKEN.search(f.label):
            return True, None
        if not f.options:                      # "your strongest coding language" in a text box: the top language only
            langs = list(profile.lookup("languages") or [])
            single = re.search(r"\b(strongest|primary|main|favou?rite|preferred|best)\b", n) and not re.search(r"languages\b", n)
            return True, (langs[0] if langs and single else None)
        got = pick_languages(list(profile.lookup("languages") or []), f.options, top_languages)
        if not got:
            return True, None
        return True, got if f.kind == FieldKind.multiselect else got[0]
    return False, None


# ---- never-LLM questions (ADR-0040) ------------------------------------------------------------------
# Legal status (export control, ITAR/EAR, "U.S. person", citizenship, immigration, authorization, sponsorship,
# clearance, criminal history) and place (residence, location, relocation, commute, on-site, hybrid, remote, time zone)
# are facts about the candidate. They are answered by the deterministic rules above or left to the owner; a model
# never guesses them, whatever the label's canonical key.
_NEVER_LLM_LEGAL = re.compile(r"export|\bitar\b|\bear\b|\bu ?s persons?\b|citizen|nationalit|\bnationals?\b|green card|"
                              r"permanent resid|clearance|sanction|embargo|immigra|\bvisas?\b|sponsor|authori[sz]|"
                              r"right to work|eligible to work|work permit|work rights|criminal|convict|felon|arrest|"
                              r"\bf ?1\b|\bh ?1 ?b\b|\bh1b\b|\bopt\b|\bcpt\b|\btn\b|\bj ?1\b|\bo ?1\b|\bl ?1\b|\bead\b|"
                              r"passport|1324b|debar|lawful|permitted to be employed|public trust")
_NEVER_LLM_PLACE = re.compile(r"resid|\blocat|\blive\b|\bliving\b|\bbased\b|relocat|commut|\bon ?site\b|\bin office\b|"
                              r"\bin person\b|hybrid|\bremote\b|time ?zones?\b|continental|contiguous|\bzip\b|postal|"
                              r"\bcity\b|\bstate\b|\bcountry\b|\baddress\b|\btravel")
# extra place wording for SHORT answers only (a textarea essay such as "Where do you see yourself in 5 years?" may still
# go to the writer; a one-line "Which office would you prefer?" never goes to a model)
_NEVER_LLM_PLACE_SHORT = re.compile(r"\boffices?\b|\bmetro\b|\bnear\b|\bwhere\b|\bmoving\b|\bmove\b|\bhq\b|"
                                    r"\bheadquarters\b|\bwork (?:in|from)\b|\barea\b|\bregion\b|\bon campus\b|"
                                    r"\bcome (?:in|to)\b")


def never_llm(f: FormField) -> bool:
    """True for legal-status and place questions (label or options): deterministic rule or owner only."""
    texts = [normalize(f.label), *(normalize(o) for o in f.options)]
    if any(_NEVER_LLM_LEGAL.search(t) or _NEVER_LLM_PLACE.search(t) for t in texts):
        return True
    return f.kind != FieldKind.textarea and any(_NEVER_LLM_PLACE_SHORT.search(t) for t in texts)


@dataclass
class MapResult:
    answers: list[Answer] = field(default_factory=list)
    unresolved: list[FormField] = field(default_factory=list)
    omitted: list[FormField] = field(default_factory=list)   # derived fields with no source (never tasks)
    # ADR-0040: optional self-identification questions left blank (no profile value, or wording not safe to answer).
    # Also in `unresolved`; the engine lists them in the plan output and never sends them to an LLM.
    skipped_voluntary: list[FormField] = field(default_factory=list)
    hints: dict[str, dict[str, Any]] = field(default_factory=dict)  # field key -> known partial facts for the owner


# ---- voluntary self-identification (ADR-0040) -----------------------------------------------------------
# Declining never states a fact, so the decline option may be chosen for any demographic-looking label.
# A real value (e.g. "No", a race) is used only when every label word is generic self-identification wording or a
# topic word of that same key: "Veteran-owned supplier?", "Gender of your emergency contact" or "Race and gender"
# get the decline option if there is one, else stay blank.
_DEMO_TOPICS: dict[str, frozenset[str]] = {
    "gender": frozenset(["gender", "genders", "sex"]),
    "hispanic": frozenset(["hispanic", "latino", "latina", "latinx", "latine", "origin"]),
    "race": frozenset(["race", "races", "racial", "ethnicity", "ethnicities", "ethnic", "background", "origin",
                       "heritage"]),
    "veteran": frozenset(["veteran", "veterans", "protected"]),
    "disability": frozenset(["disability", "disabilities", "disabled"]),
    "sexual_orientation": frozenset(["sexual", "orientation"]),
    "pronouns": frozenset(["pronoun", "pronouns"]),
}
_DEMO_GENERIC = frozenset(["a", "an", "the", "of", "or", "and", "to", "in", "is", "are", "am", "i", "you", "your", "do",
                           "does", "have", "has", "what", "which", "how", "would", "describe", "describes", "best",
                           "following", "please", "select", "choose", "identify", "identification", "self",
                           "indicate", "status", "voluntary", "optional", "required", "identity", "as", "that",
                           "apply", "applies", "all", "one", "more", "any", "if", "s", "category", "categories",
                           "classification", "classifications", "eeo", "eeoc", "my"])
# optional fields that are self-identification even without a demographic keyword (listed, never answered here)
_VOLUNTARY_LABEL = re.compile(r"self[- ]identif|voluntary|demographic|impairment|armed forces|military|lgbt|"
                              r"transgender|\bsex\b|ethnic|\braces?\b|\bracial\b", re.IGNORECASE)

# EEO isolation (ADR-0040). Classification runs FIRST in map_fields: a field whose label names any demographic category
# (any word form) is EEO, and only its [demographics] value or the decline option may answer it. No other rule
# (us_citizen, privacy_ack, authorization, prior_employee, saved [answers]) ever sees it.
_EEO_TOPIC_RES: dict[str, re.Pattern[str]] = {k: re.compile(p) for k, p in {
    "gender": r"\bgenders?\b|\btransgender\w*\b|\bsex\b",
    "hispanic": r"\bhispanics?\b|\blatin[oaxe]s?\b|\blatinx\b",
    "race": r"\braces?\b|\bracial\b|\bethnic\w*\b",
    "veteran": r"\bveterans?\b|\bmilitary\b|\barmed forces\b",
    "disability": r"\bdisab(?:ility|ilities|led)\b|\bimpairments?\b",
    "sexual_orientation": r"\bsexual orientations?\b|\blgbt\w*\b|\bqueer\b",
    "pronouns": r"\bpronouns?\b",
}.items()}
# category VALUES (option texts), used only to check chosen options, never to classify a label
_EEO_VALUE_RES: dict[str, re.Pattern[str]] = {k: re.compile(p) for k, p in {
    "gender": r"\b(?:man|woman|men|women|male|female|non ?binary|genderqueer|agender|genderfluid)\b",
    "race": r"\b(?:asian|white|black|african american|caucasian|native american|american indian|alaska native|"
            r"pacific islander|native hawaiian|two or more)\b",
    "sexual_orientation": r"\b(?:gay|lesbian|bisexual|heterosexual|straight|asexual|pansexual)\b",
    "pronouns": r"\b(?:he him|she her|they them|he they|she they)\b",
}.items()}
_EEO_GENERIC = re.compile(r"\beeoc?\b|\bself ?identif\w*|\bequal (?:employment )?opportunity\b|\bdemographics?\b")
# option domains: race and Hispanic/Latino options share one "ethnicity" domain ("Hispanic or Latino" is a race option)
_EEO_DOMAIN = {"hispanic": "ethnicity", "race": "ethnicity"}


def eeo_topics(text: str) -> list[str]:
    """Demographic categories named anywhere in `text`, in a fixed order."""
    n = normalize(str(text))
    return [k for k, rx in _EEO_TOPIC_RES.items() if rx.search(n)]


def option_topics(option: str) -> list[str]:
    """Demographic categories an option belongs to: category words plus category values ("Asian", "Woman")."""
    n = normalize(str(option))
    return list(dict.fromkeys(eeo_topics(option) + [k for k, rx in _EEO_VALUE_RES.items() if rx.search(n)]))


def eeo_class(label: str) -> tuple[bool, str | None]:
    """(is EEO/self-identification, demographic key or None when no single category can be named)."""
    topics = eeo_topics(label)
    if topics:
        return True, topics[0]
    return bool(_EEO_GENERIC.search(normalize(label))), None


def _is_category_option(o: str) -> bool:
    n = normalize(o)
    return bool(n) and n not in ("yes", "no") and not any(m in n for m in DECLINE_MARKERS)


def eeo_option_ok(option: Any, key: str | None, options: Sequence[str]) -> bool:
    """A chosen option must belong to the field's own domain: no other category's option (a disability option never
    answers a gender field) and no bare Yes/No when the field offers category options."""
    want = _EEO_DOMAIN.get(key or "", key)
    for o in option if isinstance(option, list) else [option]:
        if any(_EEO_DOMAIN.get(t, t) != want for t in option_topics(str(o))):
            return False
        if normalize(str(o)) in ("yes", "no") and any(_is_category_option(x) for x in options):
            return False
    return True


def _is_decline(v: Any) -> bool:
    nv = normalize(str(v))
    return nv in ("decline", "prefer not to say") or nv.startswith("decline")


def demographic_label_ok(label: str, ck: str) -> bool:
    """Every word is generic self-identification wording or a topic word of `ck` (never another topic)."""
    own = _DEMO_TOPICS.get(ck)
    if own is None:
        return False
    words = normalize(label).split()
    return bool(words) and bool(own & set(words)) and all(t in own or t in _DEMO_GENERIC for t in words)


def demographic_value(f: FormField, ck: str | None, profile: ProfileLike) -> Any | None:
    """The owner's [demographics] value as the exact option; the decline option when the profile declines or the
    value/wording does not match exactly; else None. Never a guessed or nearest option."""
    if not ck:
        return None
    v = profile.lookup("gender_if_required") if ck == "gender" and f.required else None
    if v in (None, ""):
        v = profile.lookup(ck)              # profile.demographics only, never free-form answers
    if v in (None, ""):
        return None
    if _is_decline(v):
        if not f.options:
            return str(v) if f.kind in (FieldKind.text, FieldKind.unknown) else None
        got = decline_option(f.options)
    elif not f.options:
        if not demographic_label_ok(f.label, ck):
            return None
        if f.kind == FieldKind.boolean:
            return _truth(v)
        return str(v) if f.kind in (FieldKind.text, FieldKind.unknown) else None
    else:   # the exact option for a plainly-worded question; otherwise declining (never a nearest guess)
        got = (exact_option(str(v), f.options) if demographic_label_ok(f.label, ck) else None) or decline_option(f.options)
    return None if got is None else [got] if f.kind == FieldKind.multiselect else got


# ---- applicant privacy-notice acknowledgement (ADR-0034) ---------------------------------------------
# Opt-in only (profile.preferences.acknowledge_privacy_notices is True). A label must be a privacy notice /
# data-processing acknowledgement AND must not touch anything in _PRIVACY_EXCLUDE; otherwise behaviour is unchanged.
_PRIVACY_LABEL = re.compile(
    r"\bprivacy (notice|policy|statement|acknowledg\w*|disclosure)\b|"
    r"\b(data|applicant|candidate|recruitment|job applicant) privacy\b|"
    r"\bdata (processing|protection) (notice|policy|statement|acknowledg\w*)\b|"
    r"\bprivacy\b.*\backnowledg|\backnowledg\w*\b.*\bprivacy\b")
_PRIVACY_EXCLUDE = re.compile(
    r"personally|\bmy own\b|\bown (work|words|effort)\b|completed (this|the) application|"          # authorship
    r"\bai\b|artificial intelligence|\bllms?\b|chat ?gpt|generative|machine learning|"               # no-AI / AI use
    r"truth|accura|\bcertif|attest|\bcorrect\b|complete and|best of my knowledge|misrepresent|falsif|"   # certifications
    r"\bbackground\b|\bdrug\b|screening|consumer report|\bfcra\b|fingerprint|credit check|criminal|"   # checks
    r"arbitrat|dispute resolution|class action|\bwaive|binding|"                                        # arbitration
    r"marketing|\bsms\b|text messag|mobile messag|newsletter|promotion|talent (community|network|pool)|"  # opt-ins
    r"future (opportunit|roles|openings)|stay in touch|job alerts?|email updates|opt ?-? ?in\b|"
    r"terms of (employment|use|service)|terms and conditions|employment (terms|agreement|contract|offer)|"  # terms
    r"conditions of employment|at will|non ?compete|confidentiality|"
    r"consent to (receive|be contacted)|contact me|\bsubscribe", re.IGNORECASE)
_ACK_OPTION = re.compile(r"\b(acknowledg\w*|agree\w*|accept\w*|confirm\w*|yes|understand\w*|read)\b")


# A field that is (or mentions) a sensitive or canonical question never takes the acknowledgement path (ADR-0038):
# "Will you require sponsorship? (See our Applicant Privacy Notice)" is a sponsorship question, not a notice.
_SENSITIVE_TOPIC = re.compile(
    r"sponsor|\bvisa\b|citizen|immigra|nationality|permanent resident|green card|right to work|work permit|"
    r"authori[sz](ed|ation) to work|eligible to work|work authori[sz]ation|"
    r"gender|\bsex\b|pronoun|ethnic|\brace\b|hispanic|latin[oax]|veteran|military|disab|sexual orientation|"
    r"transgender|\beeo\b|equal (employment )?opportunity|self[- ]identif|protected|"
    r"\bage\b|18 years|criminal|convict|salary|compensation|relocat|employed|employee")
_SENSITIVE_KEYS = SEMANTIC_SENSITIVE | DEMOGRAPHIC_KEYS | {"over_18", "located_in_us", "salary_expectation"}


def is_sensitive_question(label: str) -> bool:
    """True when any sensitive/demographic alias or topic appears anywhere in the label (alias order ignored)."""
    n = normalize(label)
    if _SENSITIVE_TOPIC.search(n) or _DEMOGRAPHIC_LABEL.search(label):
        return True
    return any(re.search(p, n) for k, pats in ALIASES if k in _SENSITIVE_KEYS for p in pats)


# Deny-by-default (ADR-0038): after the privacy phrase and generic acknowledgement words are removed, nothing
# substantive may remain. "Privacy notice: VEVRAA" leaves "vevraa" -> unresolved.
_ACK_VOCAB = frozenset(["privacy", "notice", "notices", "policy", "policies", "statement", "disclosure", "acknowledgement", "acknowledgment", "acknowledge", "acknowledged", "acknowledges", "acknowledging", "read", "reviewed", "review", "understand", "understood", "agree", "agreed", "accept", "accepted", "confirm", "confirmed", "applicant", "applicants", "candidate", "candidates", "job", "recruitment", "recruiting", "data", "processing", "protection", "personal", "information", "i", "have", "has", "the", "our", "that", "and", "a", "an", "of", "to", "by", "in", "my", "me", "you", "your", "please", "this", "we", "it", "its", "how", "use", "uses", "used", "s", "am", "do", "submitting", "application", "yes", "y", "ok", "been", "with"])


# ADR-0040 (#56): ordinary acknowledgement wording that states nothing about the candidate
_ACK_EXTRA = frozenset(["below", "above", "receipt", "received", "linked", "attached", "here", "herein", "following",
                        "at", "for", "on", "is", "are", "was", "which", "can", "be", "found", "available", "provided",
                        "collection", "notification", "required"])


def _only_ack_words(text: str) -> bool:
    return all(t in _ACK_VOCAB or t in _ACK_EXTRA for t in normalize(text).split())


_LEGAL_SUFFIX = frozenset(["inc", "llc", "ltd", "corp", "co", "corporation", "gmbh", "plc", "limited", "company"])
_NOTICE_OWNED = r"(?=(?:applicant |candidate |job applicant |recruitment |recruiting )?(?:privacy|data protection)\b)"


def strip_company_owner(text: str, company: str) -> str:
    """Normalized `text` with the posting company's full name removed ONLY where it owns the notice
    ("Acme's Privacy Notice", "Acme Applicant Privacy Policy"). Elsewhere its words stay and must pass on their own."""
    n = normalize(text)
    words = normalize(company).split()
    names = {" ".join(words)}
    while words and words[-1] in _LEGAL_SUFFIX:
        words = words[:-1]
        names.add(" ".join(words))
    for name in sorted((x for x in names if x), key=len, reverse=True):
        n = re.sub(rf"\b{re.escape(name)}(?: s)? {_NOTICE_OWNED}", "", n)
    return re.sub(r"\s+", " ", n).strip()


_ACK_STRONG = re.compile(r"\b(acknowledg\w*|agree\w*|accept\w*|confirm\w*|understand\w*|read)\b")


def is_ack_option(option: str, company: str = "") -> bool:
    """The option itself must be an acknowledgement ("I acknowledge", "Yes, I have read it", or a bare "Yes"),
    never a demographic, veteran, authorization or other sensitive choice."""
    n = normalize(option)
    if not n or is_sensitive_question(option) or has_negator(option) or any(w in n for w in DECLINE_MARKERS):
        return False
    if not _only_ack_words(strip_company_owner(option, company)):
        return False
    return bool(_ACK_STRONG.search(n)) or n in ("yes", "y")


def is_privacy_notice(label: str, stop_phrases: Sequence[str] = (), company: str = "") -> bool:
    """True only for a plain privacy-notice acknowledgement label; any excluded topic or stop phrase -> False."""
    from ..config import PolicyCfg
    default_stop = PolicyCfg.model_fields["stop_phrases"].get_default(call_default_factory=True)
    low = label.lower()
    if _PRIVACY_EXCLUDE.search(low) or any(p.lower() in low for p in (*default_stop, *stop_phrases) if p):
        return False
    stripped = strip_company_owner(label, company)
    if (canonical_key(label) not in (None, "privacy_ack") or is_sensitive_question(label)
            or not _only_ack_words(stripped)):
        return False
    return bool(_PRIVACY_LABEL.search(stripped))     # a privacy / data-protection notice noun must remain


def privacy_ack_value(f: FormField, stop_phrases: Sequence[str] = (), company: str = "") -> Any | None:
    """The affirmative answer for a privacy-notice field, or None (leave unresolved) if unsure."""
    if not is_privacy_notice(f.label, stop_phrases, company):
        return None
    if eeo_class(f.label)[0] or any(option_topics(o) or eeo_class(o)[0] for o in f.options):   # ADR-0040
        return None
    if f.kind == FieldKind.boolean:
        return True
    if f.kind in (FieldKind.select, FieldKind.multiselect) or f.options:
        if any(_PRIVACY_EXCLUDE.search(o) for o in f.options):
            return None
        aff = [o for o in f.options if _ACK_OPTION.search(normalize(o)) and is_ack_option(o, company)]
        if len(aff) != 1:
            return None
        return [aff[0]] if f.kind == FieldKind.multiselect else aff[0]
    return None


def map_fields(fields: list[FormField], profile: ProfileLike, *, referral_preference: list[str] | None = None,
               min_score: float = 0.78, company: str = "", top_languages: int = 3,
               stop_phrases: Sequence[str] = (), company_aliases: Sequence[str] = (),
               job_in_us: bool = False) -> MapResult:
    pref = DEFAULT_REFERRAL_PREFERENCE if referral_preference is None else referral_preference
    res = MapResult()
    eeo_keys: set[str] = set()

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
        is_eeo, ek = eeo_class(f.label)
        if is_eeo:                       # ADR-0040: EEO isolation, before every other rule
            v = demographic_value(f, ek, profile)
            if v is not None and f.options and not eeo_option_ok(v, ek, f.options):
                v = None
            eeo_keys.add(f.key)
            ok(f, v, f"rule:{ek or 'eeo'}")
            continue
        ck = canonical_key(f.label)
        if ck == "privacy_ack" and is_sensitive_question(f.label):   # "Gender (see our Privacy Notice)" (ADR-0038)
            ck = canonical_key(f.label, skip=("privacy_ack",))
        # opt-in (ADR-0034); only for a field that is not a sensitive or canonical question (ADR-0038)
        if ck in (None, "privacy_ack") and _truth(profile.lookup("acknowledge_privacy_notices")) is True:
            pv = privacy_ack_value(f, stop_phrases, company)
            if pv is not None:
                res.answers.append(Answer(key=f.key, value=pv, source="rule:privacy_ack"))
                continue
        if ck == "graduation" and f.kind not in (FieldKind.boolean,):
            ok(f, derive_graduation(f, profile.lookup("graduation")), "derived:graduation")
            continue
        if ck in DEMOGRAPHIC_KEYS or (ck is None and _DEMOGRAPHIC_LABEL.search(f.label)):
            res.unresolved.append(f)     # demographic alias without a category word: never answered by another rule
            continue
        if ck == "referral_source" and f.options:
            ok(f, pick_source(profile.lookup("referral_source"), f.options, pref), "rule:referral_source")
            continue
        if ck in _SEMANTIC_KEYS:
            handled, sv = _semantic(f, ck, profile, company, top_languages, company_aliases, job_in_us)
            if handled:
                if sv is None or sv == "":
                    res.unresolved.append(f)
                    if ck == "citizenship_countries" and (c := profile.lookup("citizenship_country")):
                        res.hints[f.key] = {"citizenship": str(c),
                                            "note": "profile citizenship only; the rest of the question needs the owner"}
                    continue
                if f.kind == FieldKind.multiselect and isinstance(sv, list):
                    res.answers.append(Answer(key=f.key, value=sv, source=f"rule:{ck}"))
                    continue
                if ck in ("years_experience", "languages_known", "gpa", "citizenship", "citizenship_countries") and f.options and sv in f.options:
                    res.answers.append(Answer(key=f.key, value=sv, source=f"rule:{ck}"))
                    continue
                c = _coerce(f, sv, min_score)
                if c is None or c == "":
                    res.unresolved.append(f)
                else:
                    res.answers.append(Answer(key=f.key, value=c, source=f"rule:{ck}"))
                continue
        value: Any | None = profile.lookup(ck) if ck else None
        if value is None:
            value = profile.lookup(normalize(f.label))
        coerced = _coerce(f, value, min_score) if value is not None else None
        if (coerced is not None and ck is None and not f.options and f.kind in (FieldKind.text, FieldKind.textarea)
                and normalize(str(coerced)) in ("yes", "no") and not _YESNO_STYLE.search(normalize(f.label))):
            coerced = None                       # a saved Yes/No never goes into a free-text question
        if coerced is None or coerced == "":
            res.unresolved.append(f)
        else:
            res.answers.append(Answer(key=f.key, value=coerced, source="profile" if ck is None else f"rule:{ck}"))
    # ADR-0040 option domain: outside the EEO path, a chosen option may never name a demographic category
    by_key = {f.key: f for f in fields}
    keep: list[Answer] = []
    for a in res.answers:
        f = by_key.get(a.key)
        if (f is not None and a.key not in eeo_keys and f.options
                and any(option_topics(str(x)) for x in (a.value if isinstance(a.value, list) else [a.value]))):
            res.unresolved.append(f)
        else:
            keep.append(a)
    res.answers = keep
    res.skipped_voluntary = [f for f in res.unresolved if not f.required
                             and (f.key in eeo_keys or _DEMOGRAPHIC_LABEL.search(f.label) or _VOLUNTARY_LABEL.search(f.label))]
    return res


# ---------------------------------------------------------------- education block (ADR-0029)
_LEVELS: list[tuple[re.Pattern[str], str]] = [
    (re.compile(r"\bm\.?b\.?a\b|business administration", re.IGNORECASE), "M.B.A"),
    (re.compile(r"ph\.?\s?d|doctor", re.IGNORECASE), "Doctor"),
    (re.compile(r"\bmaster|\bm\.?\s?(s|a|eng|tech)\b\.?|\bmsc\b|\bmeng\b", re.IGNORECASE), "Master"),
    (re.compile(r"\bbachelor|\bb\.?\s?(s|a|e|eng|tech)\b\.?|\bbsc\b|\bbtech\b", re.IGNORECASE), "Bachelor"),
    (re.compile(r"\bassociate", re.IGNORECASE), "Associate"),
    (re.compile(r"high school|\bged\b", re.IGNORECASE), "High School"),
]


def degree_level(value: str) -> str | None:
    return next((lvl for rx, lvl in _LEVELS if rx.search(value or "")), None)


def pick_degree(value: str, options: list[str]) -> str | None:
    """Snap a free-text degree ("Master of Science", "BS") onto an option such as "Master's Degree".
    Never crosses levels and never turns a generic master's into an MBA."""
    if not value or not options:
        return None
    if (hit := next((o for o in options if normalize(o) == normalize(value)), None)):
        return hit
    lvl = degree_level(value)
    if lvl is None:
        return best_option(value, options, cutoff=0.9)
    key = lvl.lower()
    cands = [o for o in options if key in o.lower()]
    if lvl != "M.B.A":
        cands = [o for o in cands if "m.b.a" not in o.lower() and "business administration" not in o.lower()]
    plain = [o for o in cands if re.fullmatch(rf"{re.escape(key)}(?:'?s)? degree", o.lower().strip())]
    if plain:
        return plain[0]
    return cands[0] if len(cands) == 1 else None


def degree_queries(value: str) -> list[str]:
    lvl = degree_level(value)
    return [lvl] if lvl else [value]


def pick_school(value: str, options: list[str]) -> str | None:
    """Exact (normalised) school match, else an "Other" option, else None. Never a different school."""
    if not value:
        return None
    return (next((o for o in options if normalize(o) == normalize(value)), None)
            or next((o for o in options if normalize(o) == "other"), None))


def discipline_queries(value: str) -> list[str]:
    words = [w for w in re.split(r"[\s,/&-]+", value or "") if len(w) > 2]
    return [value, *words[:1]] if value else []

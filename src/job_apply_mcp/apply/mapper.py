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
from .geo import US_STATES
from .places import (
    _VISA_COMMON,
    COUNTRY_NAMES,
    ISO2,
    MULTI_REGION,
    SUBDIVISIONS,
    VISA_LEXICON,
    VISA_MARKERS,
    _part_in,
    country_iso,
    markers_in,
    split_location,
    strip_own_subdivisions,
)
from .ports import ProfileLike

_PUNCT = re.compile(r"[^a-z0-9 ]+")
_OPTIONAL = re.compile(r"\((optional|required)\)|\b(optional|required)\b$")


# ADR-0053: misspellings of "sponsor" ("sponorship", "sponsership", "sponser") read as "sponsor", so such a question is
# RECOGNISED as a sponsorship question (and never takes a flat [answers] value). The answer source is unchanged.
_SPONSOR_TYPO = re.compile(r"\bspon(?!sor)[a-z]{0,3}(?:or|er)(s|ed|ing|ship|ships)?\b")


def normalize(label: str) -> str:
    s = label.lower().replace("’", "'")
    s = _OPTIONAL.sub(" ", s)
    s = _PUNCT.sub(" ", s)
    s = _SPONSOR_TYPO.sub(lambda m: "sponsor" + (m.group(1) or ""), s)
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
    # ADR-0055 (#86/#87 item 5): "Are you a U.S. person (ITAR/EAR)?" / "Which best describes your U.S. person status?".
    # Before every citizenship key: a U.S.-person definition names citizens and permanent residents.
    ("us_person", (r"\bu ?s persons?\b",)),
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
                        r"\bhave you (ever )?(been employed|worked) (at|by|for)\b",
                        r"^are you (?:currently|presently) (?:an? )?employee (?:of|at)\b")),   # ADR-0057
    ("marketing_optin", (r"\btalent (community|network|pool)\b", r"\bjoin (our|the|\w+ s) (talent|community|network)\b",
                         r"\b(newsletters?|marketing (emails?|communications?|messages?))\b")),
    ("first_name", (r"^(legal )?first name$", r"^given name$")),
    ("last_name", (r"^(legal |preferred )?(last|family) name$", r"^surname$")),      # ADR-0055: "Preferred Last Name"
    ("full_name", (r"^(your )?(full |legal |full legal |legal full )?name$",
                   r"^(?:your )?(?:legal |full )?first and last (?:legal )?names?$")),             # "Legal First and Last Name"
    ("preferred_name", (r"^preferred (first )?name$",)),
    ("email", (r"^e ?mail( address)?$",)),
    ("phone", (r"^(mobile |cell )?phone( number)?$", r"^mobile( number)?$")),
    ("linkedin", (r"linkedin",)),
    ("github", (r"github",)),
    ("website", (r"^(personal )?(website|portfolio)( url)?$", r"^website$", r"^portfolio")),
    ("location", (r"^(your )?(current )?location( city)?$", r"^city$", r"^current city$",
                  r"^where are you (currently )?(located|based|living|residing)$",
                  r"^where do you (currently )?(live|reside)$", r"^(your )?(current )?(city|location) of residence$")),
    ("country", (r"^country( of residence)?$",
                 # ADR-0053: "Please select the country where you currently reside", "Country of current residence"
                 (r"^(?:please (?:select|enter|choose|indicate|provide) |what is |which is )?(?:the |your )?(?:current )?"
                  r"country (?:of (?:your )?(?:current |primary )?residence|(?:where|in which) you (?:currently |presently )?"
                  r"(?:reside|live|are located|are based)(?: in)?)$"),
                 r"^(?:in )?(?:what|which) country do you (?:currently |presently )?(?:reside|live)(?: in)?$",
                 r"^(?:your )?(?:current )?country of residen(?:ce|cy)$")),
    # ADR-0057: "Which state, province, or region do you currently live in?" ([contact].region only)
    ("residence_region", ((r"^(?:in )?(?:which|what) (?:state|province|region)(?:,? (?:or |and )?(?:state|province|"
                           r"region|territory))* do you (?:currently |presently )?(?:live|reside)(?: in)?$"),
                          (r"^(?:your )?(?:current )?(?:state|province|region)(?: or (?:state|province|region))* of "
                           r"residence$"))),
    # ADR-0053: "If located in the US, in what city and state do you reside?" (from [contact] city + region only)
    ("residence_city_state", ((r"^(?:if (?:you are |you re )?(?:located|based|living|residing) in the "
                               r"(?:us|u s|usa|united states)(?: of america)? )?(?:in )?(?:what|which) city (?:and|or) "
                               r"state do you (?:currently |presently )?(?:reside|live)(?: in)?$"),
                              r"^(?:your )?(?:current )?city (?:and|or) state of residence$")),
    # ADR-0053: "current or previous employer/title" = the current [[work]] entry, else the latest ended one
    ("recent_company", ((r"^(?:(?:who|what) (?:is|was) )?(?:your )?(?:current or (?:previous|most recent|last|former|"
                         r"past)|most recent|latest) (?:company|employer)(?: name)?$"),
                        # ADR-0055: "Where have you most recently worked?"
                        r"^where (?:have|did) you (?:most recently|last) work(?:ed)?$")),
    ("recent_title", ((r"^(?:what (?:is|was) )?(?:your )?(?:current or (?:previous|most recent|last|former|past)|"
                       r"most recent|latest) (?:job )?(?:title|position)$"),)),
    ("current_company", (r"^current (company|employer)$",
                         r"^(?:(?:who|what) is )?(?:your )?current (?:company|employer)(?: name)?$")),
    ("current_title", (r"^current (job )?title$", r"^(?:what is )?(?:your )?current (?:job )?(?:title|position)$")),
    ("work_authorized", (r"authorized to work", r"legally authorized", r"eligible to work", r"work authorization",
                         r"authori[sz]ation to work", r"(permitted|allowed) to work", r"right to work")),
    ("requires_sponsorship", (r"sponsorship", r"require.* visa", r"\bsponsor(s|ed|ing)?\b",
                              r"^visa(?: status)?$")),      # ADR-0057: a bare "Visa" (confirmed home-country job only)
    ("willing_to_relocate", (r"relocat",)),
    ("open_to_hybrid", (r"\bhybrid\b",)),
    ("open_to_onsite", (r"\b(open|willing|able|comfortable|commit)\b.*\b(on site|onsite|in office|in person)\b",
                        # ADR-0055: "able to work from our office in X", "open to working out of our X Office"
                        (r"\b(open|willing|able|comfortable)\b.*\bwork(?:ing)? (?:from|in|out of|at) (?:one of )?"
                         r"(?:our|the|an?) (?:\w+ ){0,4}offices?\b"))),
    ("located_in_us", ((r"(located|based|reside|live|residing)\b.*\b(in|within) the ((continental|contiguous) )?"
                        r"(us|u s|usa|united states)\b"),)),
    # ADR-0053: "Do you live within 50 miles of Austin, TX?" / "Are you located in Canada?" (from [contact] only)
    ("located_near", ((r"^(?:are|do) you (?:currently |presently )?(?:located|based|live|living|reside|residing) "
                       r"within \d+ (?:miles?|mi|kilometers?|kilometres?|km) of .+$"),)),
    ("located_in_place", (r"^(?:are|do) you (?:currently |presently )?(?:located|based|live|living|reside|residing) in .+$",)),
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
    # ADR-0055 (#86, #87): education facts from [[education]] only (PROFILE_FACT_KEYS)
    ("recent_school", ((r"^(?:what (?:is|was) )?(?:the |your )?(?:most recent|latest|last) (?:school|university|college|"
                        r"institution)(?: (?:you|that you) (?:attended|graduated from))?$"),)),
    ("recent_degree", ((r"^(?:what (?:is|was) )?(?:the |your )?(?:most recent|latest|last) degree(?: (?:you|that you) "
                        r"(?:have )?(?:obtained|earned|completed|received|attained))?$"),)),
    ("degree_type", (r"^(?:your )?(?:highest )?degree (?:type|level)$",
                     (r"^(?:what is )?(?:your )?highest (?:level of education|education level|degree)(?: (?:you have )?"
                      r"(?:obtained|earned|completed|attained))?$"),)),
    ("last_internship", (r"^(?:where|what) (?:was|is) (?:your )?(?:last|latest|most recent) internship(?: company)?$",
                         r"^(?:last|latest|most recent) internship(?: company| employer)?$")),
    ("school", (r"^school( name)?$", r"^university$", r"^college$",
                r"^(?:university|college|school)(?: or (?:university|college|school))*(?: attended)?$")),
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
_PREF_FIELD = {"relocation_support": "needs_relocation_support"}
_SEMANTIC_KEYS = {"work_authorized", "requires_sponsorship", "us_citizen", "relocation_support", "open_to_hybrid",
                  "open_to_onsite", "knows_employee", "years_experience", "languages_known", "citizenship", "gpa",
                  "prior_employee", "marketing_optin", "citizenship_countries", "located_in_us", "willing_to_relocate"}
# Never sent to the local LLM: a guess is a false statement about the candidate.
# ADR-0053: facts read from [contact] / [[work]] only; unresolved (and a profile gap) when the fact is missing
PROFILE_FACT_KEYS = frozenset(["country", "residence_city_state", "recent_company", "recent_title", "current_company",
                               "current_title", "located_near", "located_in_place",
                               # ADR-0055: [[education]] / [[work]] facts (#86, #87)
                               "recent_school", "recent_degree", "degree_type", "last_internship",
                               "residence_region"])                         # ADR-0057
SEMANTIC_SENSITIVE = _SEMANTIC_KEYS | {"willing_to_relocate"} | PROFILE_FACT_KEYS
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
_US_IN_LABEL = re.compile(r"\b(u s(?: a)?|usa|united states( of america)?|america|american)\b")
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


# question-pattern names -> ISO-2 (None = a region such as the EU: never one country)
_NAME_ISO: dict[str, str | None] = {
    "united kingdom": "GB", "canada": "CA", "ireland": "IE", "european union": None, "germany": "DE", "france": "FR",
    "netherlands": "NL", "spain": "ES", "italy": "IT", "portugal": "PT", "poland": "PL", "sweden": "SE", "norway": "NO",
    "denmark": "DK", "finland": "FI", "switzerland": "CH", "austria": "AT", "belgium": "BE", "luxembourg": "LU",
    "czechia": "CZ", "romania": "RO", "ukraine": "UA", "greece": "GR", "hungary": "HU", "estonia": "EE",
    "turkey": "TR", "israel": "IL", "united arab emirates": "AE", "india": "IN", "pakistan": "PK", "china": "CN",
    "hong kong": "HK", "taiwan": "TW", "japan": "JP", "south korea": "KR", "singapore": "SG", "philippines": "PH",
    "vietnam": "VN", "indonesia": "ID", "malaysia": "MY", "thailand": "TH", "australia": "AU", "new zealand": "NZ",
    "mexico": "MX", "brazil": "BR", "argentina": "AR", "colombia": "CO", "chile": "CL", "peru": "PE",
    "costa rica": "CR", "south africa": "ZA", "nigeria": "NG", "kenya": "KE", "egypt": "EG",
}
_ISO_NAME = {v: k for k, v in _NAME_ISO.items() if v}

# Deny-by-default (ADR-0038/0040): a country's facts answer only a label whose every word is generic
# authorization/sponsorship wording, that country's visa lexicon, or the country itself (replaced by a placeholder).
# Any other word (a city, region, another country, "here", "the country where ...") leaves it unresolved.
_PLACE = "zzplacezz"
_AUTH_VOCAB_GENERIC = frozenset(["a", "an", "the", "and", "or", "any", "to", "in", "of", "for", "with", "without", "by", "on", "at", "as", "is", "are", "be", "been", "being", "do", "does", "did", "will", "would", "can", "could", "may", "must", "shall", "should", "if", "not", "no", "now", "currently", "presently", "future", "ever", "you", "your", "yourself", "i", "am", "have", "has", "had", "who", "that", "this", "legally", "lawfully", "legal", "authorized", "authorised", "authorization", "authorisation", "authorize", "unauthorized", "eligible", "eligibility", "permitted", "allowed", "able", "right", "work", "working", "works", "employment", "employed", "employer", "employers", "job", "position", "role", "require", "requires", "required", "requiring", "need", "needs", "needed", "sponsor", "sponsors", "sponsored", "sponsorship", "sponsoring", "visa", "visas", "status", "immigration", "immigrant", "petition", "support", "assistance", "obtain", "maintain", "retain", "extend", "continue", "continued", "extension", "transfer", "valid", "permit", "current", "based", "restriction", "restrictions", "restricted", "unrestricted", "basis", "full", "time", "hired", "offered", "offer", "company", "please", "select", "answer", "yes", "no", "question", "choose", "option", "options", "describes", "describe", "best", "which", "what", "following", "one", "either", "our", _PLACE])
_US_WORDS = frozenset(["us", "u", "s", "usa", "united", "states", "america", "american"])
_AUTH_VOCAB = _AUTH_VOCAB_GENERIC | VISA_LEXICON["US"] | _US_WORDS      # ADR-0038 US vocabulary (compatibility)
_US_NAME_IN_QUESTION = re.compile(r"\b(?:united states(?: of america)?|usa|u s(?: a)?|america|american|us)\b")


def auth_place_ok(label: str) -> bool:
    """True when the label names no place, or only the US (US, U.S., USA, United States, America)."""
    return all(t in _AUTH_VOCAB or t.isdigit() for t in normalize(label).split())


_EXAMPLE_CLAUSE = re.compile(
    r"\(\s*(?:e\.?\s?g\.?|i\.?\s?e\.?|ex\.?|such as|like|for example|including)\s*[,:]?\s*([^()]*)\)|"
    r"[,;:]?\s*\b(?:e\.\s?g\.|i\.\s?e\.|such as|for example|including)\s*[,:]?\s*([^.?;()]*)", re.IGNORECASE)
_VISA_TYPES = VISA_MARKERS["US"]
_AUTH_NOTE_VOCAB = frozenset(["if", "qualify", "qualifies", "defined", "define", "regulations", "note", "considered",
                              "applicants", "candidates", "those", "under", "per", "etc", "e", "g", "sometimes",
                              "called", "also", "known"])
_JOB_PLACE = re.compile(r"\b(?:the|this) country (?:where|in which) (?:the|this) (?:job|role|position) is "
                        r"(?:located|based)\b|\bthis country\b|\bthis location\b|\bthe country of employment\b|"
                        # ADR-0053 variants
                        r"\bthe country (?:where|in which) (?:this|the) (?:job|role|position) (?:will be|is to be) "
                        r"(?:located|based|performed)\b|\b(?:the|this) (?:job|role|position) s country\b|"
                        r"\bthe country (?:of|for) (?:this|the) (?:job|role|position)\b|"
                        # ADR-0057: "the country of origin for this position"
                        r"\bthe country of origin for (?:this|the) (?:job|role|position)\b|"
                        # ADR-0055: "in (the) country in which the job you're applying for is located"
                        r"\b(?:the )?country (?:where|in which) the (?:job|role|position) (?:(?:that )?you(?:'|’| a)?re "
                        r"applying (?:for|to) )?is (?:located|based)\b",
                        re.IGNORECASE)


# ADR-0057: generic words inside a visa example ("(e.g., H-1B or other employment-based visa sponsorship)")
_EXAMPLE_GENERIC = frozenset(["other", "employment", "based", "immigration", "case", "cases", "sponsorship", "petition"])
# ADR-0057: wordings that only rephrase "sponsor"
_AUTH_REWRITES = [
    (re.compile(r"\bcommence\s*\(?\s*[\"“'‘]?\s*sponsor\s*[\"”'’]?\s*\)?\s*an immigration case\b", re.IGNORECASE), "sponsor"),
    (re.compile(r"\bin order to employ you\b", re.IGNORECASE), " "),
]


def _drop_examples(text: str, country: str | None = "US") -> str:
    """Drop example clauses that list only `country`'s visa types (None: drop every example clause, for detection)."""
    def sub(m: re.Match[str]) -> str:
        inner = normalize(m.group(1) if m.group(1) is not None else m.group(2) or "").split()
        if country is None:
            return " "
        lex, marks = VISA_LEXICON.get(country, frozenset()), VISA_MARKERS.get(country, frozenset())
        visa = any(t in marks for t in inner)
        return " " if inner and visa and all(t in lex or t in _VISA_COMMON or t in _EXAMPLE_GENERIC or t.isdigit()
                                             for t in inner) else m.group(0)
    return _EXAMPLE_CLAUSE.sub(sub, text)


def _country_name_rx(country: str) -> re.Pattern[str] | None:
    if country == "US":
        return _US_NAME_IN_QUESTION
    name = _ISO_NAME.get(country)
    return _COUNTRY_RES[name] if name else None


def auth_core(label: str, country: str = "US", job_place: bool = False) -> str | None:
    """The authorization/sponsorship question about `country` inside `label` (normalized, the country replaced by a
    placeholder), or None when it may be about another place. `job_place`: "this country" / "the country where the job
    is located" refer to `country` (the job's confirmed country)."""
    for rx, rw in _AUTH_REWRITES:
        label = rx.sub(rw, label)
    text = _drop_examples(label, country)
    if job_place:
        text = _JOB_PLACE.sub(_PLACE, text)
    q, _, note = text.partition("?")
    vocab = _AUTH_VOCAB_GENERIC | VISA_LEXICON.get(country, frozenset())
    rx = _country_name_rx(country)

    def ok(part: str, extra: frozenset[str] = frozenset()) -> bool:
        n = normalize(part)
        if rx is not None:
            n = rx.sub(_PLACE, n)
        return all(t in vocab or t in extra or t.isdigit() for t in n.split())
    if note.strip() and (question_countries(note)[1] or not ok(note, _AUTH_NOTE_VOCAB)):
        return None
    return q if ok(q) else None


# The US named as the PLACE of the work: "in the US", "within the United States", "in the U.S.A.". Visa examples
# ("e.g., H-1B") and "work for US" (the company) never count.
_US_PLACE = re.compile(r"\b(?:in|within|inside) (?:the )?(?:U\.?S\.?A?\.?(?![A-Za-z])|united states(?: of america)?\b|"
                       r"america\b)", re.IGNORECASE)


def names_us(text: str) -> bool:
    """The text names the United States as a place ("... to work in the US?"), after example clauses are dropped."""
    t = _drop_examples(text or "", None)
    for m in _US_PLACE.finditer(t):
        hit = m.group(0)
        if re.search(r"united states|america", hit, re.IGNORECASE) or re.search(r"U\.?S", hit):   # "US" in capitals only
            return True
    return False


def named_countries(label: str) -> list[str | None]:
    """Countries a question names as the place of work (ISO-2; None for a region such as the EU)."""
    t = _drop_examples(label or "", None)
    out: list[str | None] = ["US"] if names_us(t) or re.search(r"\b(?:united states|usa)\b", t, re.IGNORECASE) else []
    out += [_NAME_ISO.get(c) for c in question_countries(t)[1]]
    return list(dict.fromkeys(out))


# ---- job country (ADR-0040): one strict, deny-by-default rule for any country ------------------------------------
def location_in_country(location: str, country: str) -> bool:
    """True only when every part of a location is explicitly in `country` (its name, a full subdivision name, or a
    "City, XX" subdivision code; ambiguous codes need a listed city). No location, "Remote" or another country -> False."""
    parts = split_location(location)
    for p in parts:
        foreign = [c for c in question_countries(strip_own_subdivisions(p, country))[1] if _NAME_ISO.get(c) != country]
        if not _part_in(p, country, foreign):
            return False
    return bool(parts)


def location_is_us(location: str) -> bool:
    """Alias: location_in_country(location, "US")."""
    return location_in_country(location, "US")


def job_country(location: str, countries: Sequence[str] = (), locations: Sequence[str] = ()) -> str | None:
    """ISO-2 of the job, or None. A country only if (a) the structured adapter countries are exactly one ISO-2 code
    (every location entry parsed; malformed, country-less or extra entries are "UNCONFIRMED"), AND (b) every location
    string (posting location, offices, allLocations, secondaryLocations) is in that country, AND (c) none names a
    region or several places. No text fallback, and text never overrides a structured value."""
    cs = list(dict.fromkeys(countries))
    if len(cs) != 1 or cs[0] not in ISO2:
        return None
    code = cs[0]
    texts = [str(t) for t in (location, *locations) if str(t or "").strip()]
    strict = [not MULTI_REGION.search(t) and (location_in_country(t, code) or (code == "US" and _us_hub(t)))
              for t in texts]
    if all(strict):                       # no location text at all: the structured country alone (ADR-0040)
        return code
    # ADR-0055: a company office entry that names no place we can parse ("San Jose Office (HQ)") defers to the single
    # structured country, but only beside a confirmed entry and only when it names no other country or region
    if any(strict) and all(ok or (_OFFICE_ENTRY.search(t) and not MULTI_REGION.search(t) and not markers_in(t)
                                  and not question_countries(t)[1]) for ok, t in zip(strict, texts, strict=True)):
        return code
    return None


_OFFICE_ENTRY = re.compile(r"\b(?:office|offices|hq|headquarters)\b", re.IGNORECASE)
_LOC_NOISE = re.compile(r"\b(?:greater|metro(?:politan)?|bay|area|office|offices|hq|headquarters|data center|campus|"
                        r"downtown|hybrid|on ?site|in office)\b")             # "Hybrid - Denver" (ADR-0057)


def _us_hub(text: str) -> bool:
    """ADR-0055: the last comma part is a major US city whose name belongs to one state ("Union Square, New York City",
    "Los Angeles - Greater Area", "San Francisco")."""
    comps = [normalize(c) for c in re.sub(r"\([^)]*\)", " ", text).split(",") if normalize(c)]
    if not comps:
        return False
    return re.sub(r"\s+", " ", _LOC_NOISE.sub(" ", comps[-1])).strip() in _HUB_REGION


def job_is_us(location: str, countries: Sequence[str] = (), locations: Sequence[str] = ()) -> bool:
    """Alias: job_country(...) == "US"."""
    return job_country(location, countries, locations) == "US"


# ADR-0040 (control review): an authorization/sponsorship rule never answers a field whose label, help text or options
# mention export control or a legal status other than work authorization. "Authorized to work in the US? (U.S. persons
# as defined under ITAR/EAR)" is an export-control question; it stays with the owner (and never goes to an LLM).
_AUTH_LEGAL_BLOCK = re.compile(r"\bitar\b|\bear\b|\bu ?s persons?\b|\bexport|\bcitizen\w*|permanent resident\w*|"
                               r"green card|\bclearance\b|deemed export")


def auth_legal_blocked(f: FormField) -> bool:
    return any(_AUTH_LEGAL_BLOCK.search(normalize(t)) for t in (f.label, *f.options))


_FLAT_KEY = {"work_authorized": "work_authorized", "requires_sponsorship": "requires_sponsorship", "citizen": "us_citizen",
             "requires_sponsorship_now": "requires_sponsorship_now"}
# ADR-0053: a sponsorship question about NOW only ("Do you currently require sponsorship?"). requires_sponsorship means
# now OR in the future, so only requires_sponsorship_now (or requires_sponsorship = false) answers it.
_NOW_ONLY = re.compile(r"\b(?:now|currently|presently|at (?:this|the present) time|today|at present)\b")
_FUTURE = re.compile(r"\bfuture\b|\bever\b|\blater\b|\bgoing forward\b|\bat any (?:point|time)\b|\bwill (?:you )?(?:ever )?"
                     r"(?:need|require)\b|\bupcoming\b|\bduring (?:your|the) employment\b|\bnext\b")


def country_fact(profile: ProfileLike, country: str, fact: str) -> bool | None:
    """The profile's per-country fact; the flat [authorization] keys count for the home country only."""
    v = _truth(profile.lookup(f"auth:{country}:{fact}"))
    home = str(profile.lookup("home_country") or "").upper() or None
    if v is None and country == home and not (fact == "citizen" and home != "US"):
        v = _truth(profile.lookup("structured:" + _FLAT_KEY[fact]))   # ADR-0050: never a flat [answers] pattern
    return v


# ADR-0050: wording that names only the posting company or the places picked in an earlier question. On a confirmed
# home-country job it does not change which country the question is about.
_SELECTED_PLACES = re.compile(r"\b(?:for|in|to work in) (?:the )?locations? (?:s )?(?:that )?you (?:have )?selected"
                              r"(?: (?:in )+(?:your|the) previous (?:response|answer|question))?")


# ADR-0055 (#89): a leading company-policy sentence before the question ("Lambda may agree to sponsor individuals to
# obtain work authorization (such as H-1B, TN or O-1 visa status)."). Dropped only when it is exactly this statement and
# any example clause lists only the country's own visa types.
_SPONSOR_PREAMBLE = re.compile(r"company (?:may|will|can|does|is able to) (?:(?:agree|choose|be able) to )?sponsor "
                               r"(?:individuals|candidates|applicants|employees|people|some (?:candidates|roles))"
                               r"(?: (?:to obtain|for) (?:work|employment) authori[sz]ation)?")


def _home_job_core(label: str, country: str, company: str) -> str | None:
    """auth_core() after the company's name and "the location(s) you selected" are removed (home-country job only)."""
    words = [w for w in normalize(company).split() if w not in _LEGAL_SUFFIX and w not in _AUTH_VOCAB_GENERIC]

    def strip_co(text: str) -> str:
        n = _SELECTED_PLACES.sub(" ", normalize(text))
        if words:
            n = re.sub(rf"\b{re.escape(' '.join(words))}(?: s)?\b", " company ", n)
        n = re.sub(r"\s+", " ", n).strip()
        return re.sub(r"\bfrom company\b", "by company", n)       # "require sponsorship from <company>"
    head, sep, tail = label.strip().partition(". ")
    if sep and "?" not in head and _SPONSOR_PREAMBLE.fullmatch(strip_co(_drop_examples(head, country))):
        label = tail
    q, mark, note = label.partition("?")           # keep the question / note split for auth_core
    return auth_core(strip_co(q) + (mark + " " + strip_co(note) if mark else ""), country)


def _auth_answer(f: FormField, ck: str, profile: ProfileLike, job_country: str | None = None,
                 gaps: list[dict[str, Any]] | None = None, company: str = "") -> tuple[bool, Any]:
    status_opts = auth_legal_blocked(f)
    if status_opts and (_AUTH_LEGAL_BLOCK.search(normalize(f.label)) or not f.options or _yes_no_options(f.options)):
        return True, None
    # ADR-0057: a plain sponsorship question whose OPTIONS list legal statuses ("Citizen", "Permanent Resident",
    # "Visa Holder", "Require Sponsorship") may take only the sponsorship option, and only when sponsorship is required
    named = named_countries(f.label)
    job_place = bool(_JOB_PLACE.search(f.label))
    if len(named) > 1 or (named and named[0] is None) or (named and job_place and named[0] != job_country):
        if gaps is not None:                                 # several places, a region, or a contradiction
            gaps.append({"country": None, "fact": None, "key": f.key})
        return True, None
    country = named[0] if named else job_country
    if country is None:                                      # place-less and the job's country is not confirmed
        if gaps is not None:                                 # ADR-0040: no reusable gap key for this context
            gaps.append({"country": None, "fact": None, "key": f.key})
        return True, None
    core = auth_core(f.label, country, job_place=job_place and country == job_country)
    home = str(profile.lookup("home_country") or "").upper() or None
    if core is None and not named and country == job_country == home and company:
        core = _home_job_core(f.label, country, company)
    elif core is None and named == [home] and company and (
            job_country == home or (job_country is None and names_us(f.label) and home == "US")):
        # ADR-0057 (#54 explicit-country rule): "authorized to work lawfully in the United States for <company>" names
        # the place itself, so an unconfirmed job country does not matter; a confirmed OTHER country still does
        # ADR-0053: on a confirmed home-country job, a question that also names that country ("Will <company> need to
        # sponsor you to work in the US?"): the company's name is not a place word
        core = _home_job_core(f.label, country, company)
    if core is None:
        name = _ISO_NAME.get(country)
        if name and named:                                   # only the user's own [answers] pattern naming that country
            return True, profile.lookup(f"country_answer:{name}:{normalize(f.label)}")
        return True, None
    n = normalize(core)
    auth, req = country_fact(profile, country, "work_authorized"), country_fact(profile, country, "requires_sponsorship")
    cit = country_fact(profile, country, "citizen")

    def missing(fact: str) -> tuple[bool, Any]:
        if gaps is not None:
            gaps.append({"country": country, "fact": fact, "key": f.key})
        if country != home and (name := _ISO_NAME.get(country)) and named:
            return True, profile.lookup(f"country_answer:{name}:{normalize(f.label)}")
        return True, None                    # ADR-0050: never a fall-through to flat [answers]
    ma, ms = _AUTH_WORDS.search(n), re.search(r"sponsor|\bvisa\b", n)
    has_auth, has_sp = bool(ma), bool(ms)
    if ma and ms and ms.start() < ma.start():    # "require sponsorship ... for work authorization": a sponsorship question
        has_auth = False
    if status_opts and not (has_sp and not has_auth and req is True):
        return True, None
    if f.options and not _yes_no_options(f.options):
        if has_auth:
            return True, pick_auth_option(f.options, auth, req, cit)
        if has_sp and req is True:           # ADR-0057: ["Citizen", "Permanent Resident", "Visa Holder", "Require Sponsorship"]
            hit = [o for o in f.options if re.search(r"sponsor", o, re.IGNORECASE) and not _NO_SPONSOR.search(o.lower())]
            return True, (hit[0] if len(hit) == 1 else None)
        return (True, None) if has_sp else (False, None)
    if has_auth and has_sp:                  # compound: only the explicit "authorized ... without sponsorship" form
        if _SPONS_NEG.search(n) and auth is not None and req is not None:
            return True, _yn(auth and not req)
        if auth is None or req is None:
            missing("work_authorized" if auth is None else "requires_sponsorship")
        return True, None
    if ck == "requires_sponsorship" or (has_sp and not has_auth):
        if _NOW_ONLY.search(n) and not _FUTURE.search(n) and req is not False:
            now = country_fact(profile, country, "requires_sponsorship_now")
            if now is None:
                return missing("requires_sponsorship_now")
            req = now
        if req is None:
            return missing("requires_sponsorship")
        return True, _yn(not req if _SPONS_NEG.search(n) else req)
    if re.search(r"\b(?:require|need)s?\b(?: \w+){0,2} (?:work |employment )?authori[sz]ation\b", n):
        # ADR-0057: "Do you REQUIRE work authorization ...?" asks whether the owner lacks it: Yes when not authorized,
        # No only when authorized AND no sponsorship is needed; authorized-now-but-sponsorship-later is the owner's call
        if auth is None:
            return missing("work_authorized")
        if auth is False:
            return True, "Yes"
        if req is None:
            return missing("requires_sponsorship")
        return True, ("No" if req is False else None)
    if auth is None:
        return missing("work_authorized")
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
# ADR-0055 (#87): "... in the past in a Full-time, Part-time, contractor or Intern capacity" (capacity words only)
_CAP = r"(?:full time|part time|contract(?:or)?|intern(?:ship)?|temporary|consultant|seasonal|freelance|employee)"
_WHEN = (r"(?: before| previously| in the past| at any (?:point|time)| for any (?:length|period) of time)?"
         rf"(?: in (?:a |an )?{_CAP}(?: (?:or |and )?{_CAP})+ capacity)?")   # a listed capacity set, never "any capacity"
_DIRECT_EMPLOY = re.compile(
    rf"^(?:have|did|do) you {_TIME}(?:been {_TIME}employed|worked|work|been an? (?:employee|intern)) {_TIME}(?:by|for|at|with) {_CO}"
    rf"(?: {_TIME}|{_WHEN})?$|"
    rf"^(?:are|were) you {_TIME}(?:been )?employed (?:by|at) {_CO}{_WHEN}$|"
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


_AFFILIATE_TAIL = re.compile(r" (?:or|and(?:/or)?) (?:an? |any |one of )?(?:\S+ ){0,4}(?:s )?(?:affiliates?|subsidiar(?:y|ies)|"
                             r"affiliated (?:company|companies|entity|entities))(?: of \S+)?$")


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


_CURRENT_EMPLOYEE = re.compile(rf"^are you (?:currently|presently) (?:an? )?employee (?:of|at) (?:an? )?{_CO}"
                               r"(?: (?:agency|company|affiliate|subsidiary|entity|brand|group company|office))?$")


def _mark_all(n: str, company: str, aliases: Sequence[str] = ()) -> str:
    words = _SELF_REF.sub(_CO, n).split()
    for name in [x for x in (company, *aliases) if normalize(x)]:
        found = True
        while found:
            words, found = _mark_company(words, name)
    return " ".join(words)


def _has_current_ambiguity(profile: ProfileLike) -> bool:
    """Several open-ended [[work]] entries: the current employer is ambiguous (work:current_company is None)."""
    return bool(profile.lookup("work:several_current"))


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


# ---- residence, location and employer facts (ADR-0053) ----------------------------------------------------------
# Read from [contact] and [[work]] only. A missing fact leaves the field unresolved and records a profile gap; a
# place the rule cannot compare exactly (a metro area, "near", two places, an unknown city) stays unresolved.
_AMBIGUOUS_STATE_NAMES = frozenset(["new york", "washington"])     # also city names: "located in New York?"
_US_STATE_NAMES = {normalize(v): k for k, v in US_STATES.items()}
_IF_IN_US = re.compile(r"^if (?:you are |you re )?(?:located|based|living|residing) in the (?:us|u s|usa|united states)\b")


def _region_code(region: str) -> str | None:
    r = normalize(region)
    return r.upper() if r.upper() in US_STATES else _US_STATE_NAMES.get(r)


def _place_answer(place: str, profile: ProfileLike, near: bool, gap: Any) -> Any | None:
    """Yes/No for "located in <place>" (country, US state or the owner's own city), Yes for "within N miles of <the
    owner's own city>"; anything else None. `place` is the raw text after "in"/"of"."""
    city, region = str(profile.lookup("contact:city") or ""), str(profile.lookup("contact:region") or "")
    home = country_iso(profile.lookup("country") or "")
    raw = re.sub(r"[?.!]+\s*$", "", place).strip()
    x = normalize(re.sub(r"^the\s+", "", raw, flags=re.IGNORECASE))
    if not x:
        return None
    parts = [normalize(t) for t in raw.split(",") if normalize(t)]
    if city and parts and parts[0] == normalize(city):
        # ADR-0053 (control): the owner's own city counts only with the profile's region (state/province) named; a
        # country is accepted only after that region ("Springfield, IL, USA"). A bare "Springfield" or "Springfield,
        # USA" may be another Springfield. Only a home country without subdivisions accepts "<city>, <country>".
        rest = parts[1:]
        if not rest or len(rest) > 2:
            return None

        def same_region(r: str) -> bool:
            return bool(region) and (r == normalize(region) or (_region_code(r) is not None
                                                                and _region_code(r) == _region_code(region)))

        def same_country(r: str) -> bool:
            return home is not None and country_iso(r, allow_codes=False) == home
        if region or home is None or home in SUBDIVISIONS:
            ok = same_region(rest[0]) and (len(rest) == 1 or same_country(rest[1]))
        else:
            ok = len(rest) == 1 and same_country(rest[0])
        return "Yes" if ok else None
    if near:                                                  # no geocoder: another place is never "computable"
        if not city:
            gap("contact.city")
        return None
    iso = country_iso(x, allow_codes=False)
    if iso:
        if not home:
            gap("contact.country")
            return None
        return _yn(iso == home)
    st = x.removesuffix(" state")
    if st in _US_STATE_NAMES and (st not in _AMBIGUOUS_STATE_NAMES or x.endswith(" state")):
        if not home:
            gap("contact.country")
            return None
        if home != "US":
            return "No"
        mine = _region_code(region)
        if mine is None:
            gap("contact.region")
            return None
        return _yn(mine == _US_STATE_NAMES[st])
    if not city:
        gap("contact.city")
    return None


def _profile_fact(f: FormField, ck: str, profile: ProfileLike, gaps: list[dict[str, Any]] | None) -> Any | None:
    def gap(fact: str) -> None:
        if gaps is not None:
            gaps.append({"country": None, "fact": fact, "key": f.key, "profile_fact": True})
    n = normalize(f.label)
    choice = bool(f.options) or f.kind in (FieldKind.select, FieldKind.multiselect)
    if ck in ("recent_company", "recent_title", "current_company", "current_title"):
        if choice or f.kind not in (FieldKind.text, FieldKind.textarea, FieldKind.unknown):
            return None
        v = profile.lookup("work:" + ck)
        if not v:
            gap("work.current" if ck.startswith("current") else "work")
        return v or None
    if ck == "country":
        c = str(profile.lookup("country") or "").strip()
        if not c:
            gap("contact.country")
            return None
        if not f.options:
            return c if f.kind in (FieldKind.text, FieldKind.unknown, FieldKind.location) else None
        iso = country_iso(c)
        hit = [o for o in f.options if iso and country_iso(o) == iso]
        if len(hit) > 1:
            return None
        if not hit:
            return c                           # no option names the country exactly: the usual option snapping
        return hit if f.kind == FieldKind.multiselect else hit[0]
    if ck == "residence_region":            # ADR-0057: the state/province from [contact], options by name or code
        region = str(profile.lookup("contact:region") or "").strip()
        if not region:
            gap("contact.region")
            return None
        code = _region_code(region)
        names = {normalize(region)} | ({code.lower(), normalize(US_STATES[code])} if code else set())
        if choice:
            hit = [o for o in f.options if normalize(o) in names]
            if len(hit) != 1:
                return None
            return [hit[0]] if f.kind == FieldKind.multiselect else hit[0]
        return US_STATES[code] if code else region
    if ck == "residence_city_state":
        if choice:
            return None
        city, region = str(profile.lookup("contact:city") or ""), str(profile.lookup("contact:region") or "")
        if _IF_IN_US.search(n):
            home = country_iso(profile.lookup("country") or "")
            if home is None:
                gap("contact.country")
                return None
            if home != "US":
                return None                     # the question is for US residents only: the owner decides
        if not city or not region:
            gap("contact.city" if not city else "contact.region")
            return None
        return f"{city}, {region}"
    if ck in ("recent_school", "last_internship"):
        v = profile.lookup("edu:recent_school" if ck == "recent_school" else "work:last_internship")
        if not v:
            gap("education" if ck == "recent_school" else "work.internship")
            return None
        if choice:
            return pick_school(str(v), f.options) if ck == "recent_school" and f.kind == FieldKind.select else None
        return str(v) if f.kind in (FieldKind.text, FieldKind.textarea, FieldKind.unknown) else None
    if ck in ("recent_degree", "degree_type"):
        # ADR-0055: "Degree Type" / "highest degree" = the highest level; "most recent degree" = the latest entry, and
        # with "obtained/earned/completed" only an entry whose end date has passed
        done = bool(re.search(r"\b(?:obtained|earned|completed|received|attained)\b", n))
        v = profile.lookup("edu:highest_degree" if ck == "degree_type" else
                           "edu:recent_degree_completed" if done else "edu:recent_degree")
        if not v:
            gap("education")
            return None
        if not f.options:
            return str(v) if f.kind in (FieldKind.text, FieldKind.unknown) else None
        hit = pick_degree(str(v), f.options)
        if hit is None:
            return None
        return [hit] if f.kind == FieldKind.multiselect else hit
    if ck in ("located_near", "located_in_place"):
        if f.options and not _yes_no_options(f.options) or f.kind not in (FieldKind.boolean, FieldKind.select,
                                                                         FieldKind.text, FieldKind.unknown):
            return None
        m = re.search(r"\bwithin\s+\d+\s*(?:miles?|mi|kilometers?|kilometres?|km)\s+of\s+(.+)$", f.label, re.IGNORECASE) \
            if ck == "located_near" else re.search(r"\b(?:located|based|live|living|reside|residing)\s+in\s+(.+)$",
                                                    f.label, re.IGNORECASE)
        return _place_answer(m.group(1), profile, ck == "located_near", gap) if m else None
    return None


# ---- residence coupled with hybrid / on-site / relocation (ADR-0055, #85) ---------------------------------------
# "Yes, I'm based in this location and able to work from the office 3 days per week" asserts RESIDENCE, not only a
# work preference. Such an option is chosen only when the profile's city + region match the posting location; the
# "not based here but willing to relocate" option only when the posting is confirmed elsewhere (another region or
# country) and [preferences].willing_to_relocate is true. Anything else stays with the owner.
_RES_HERE = re.compile(r"\b(?:based|located|living|live|reside|residing|local)\b (?:(?:in|near|to|within|at) )"
                       r"(?:the |this |our |your |an |a )?(?:listed |posted |same |job s |role s )?(?:location|area|city|office|"
                       r"offices|region|metro(?: area)?|commuting distance|hub)\b|\bbased (?:here|locally)\b|\blocal to\b|"
                       r"\b(?:live|reside|based) nearby\b")
_RES_OFFICE = re.compile(r"\boffice\b|\bin person\b|\bon ?site\b|\bhybrid\b|\bdays? (?:a|per) week\b|\bcommut")
_RES_ONSITE = re.compile(r"\bon ?site\b|\b5 days\b|\bfive days\b|\bfull time in (?:the )?office\b")
_RES_HYBRID = re.compile(r"\bhybrid\b|\b[1-4] days?\b|\b(?:one|two|three|four) days?\b")
# major US cities whose bare name names exactly one state ("San Francisco", "New York": city or state, NY either way)
_HUB_REGION = {"san francisco": "CA", "los angeles": "CA", "san diego": "CA", "palo alto": "CA",
               "mountain view": "CA", "sunnyvale": "CA", "new york": "NY", "new york city": "NY", "nyc": "NY",
               "seattle": "WA", "chicago": "IL", "boston": "MA", "austin": "TX",
               "dallas": "TX", "houston": "TX", "denver": "CO", "atlanta": "GA", "philadelphia": "PA",
               "pittsburgh": "PA", "phoenix": "AZ", "salt lake city": "UT", "miami": "FL",
               "minneapolis": "MN", "detroit": "MI", "nashville": "TN", "raleigh": "NC", "baltimore": "MD"}


_CITY_ALIAS = {"new york city": "new york", "nyc": "new york"}


def _loc_region(loc: str) -> tuple[str | None, str | None, str | None]:
    """(city, region code or name, ISO-2 country) of one posting location, as far as it is stated; None = unknown."""
    parts = [re.sub(r"\s+", " ", _LOC_NOISE.sub(" ", normalize(t))).strip()
             for t in re.sub(r"\([^)]*\)", " ", loc).split(",")]
    parts = [t for t in parts if t]                                 # "Los Angeles - Greater Area" -> "los angeles"
    if not parts or MULTI_REGION.search(loc) or re.search(r"\bremote\b", loc, re.IGNORECASE):
        return None, None, None
    m = re.fullmatch(r"([A-Z]{2})-([A-Za-z .'-]+)", loc.strip())    # Ashby "GB-London" / "US-Seattle"
    if m and country_iso(m.group(1)):
        return normalize(m.group(2)), None, country_iso(m.group(1))
    if len(parts) >= 2 and parts[-1] in _HUB_REGION:                # "Union Square, New York City"
        return parts[-1], _HUB_REGION[parts[-1]], "US"
    country = country_iso(parts[-1], allow_codes=False) if len(parts) >= 2 else None
    rest = parts[:-1] if country else parts
    city = rest[0] if rest else None
    region = rest[1] if len(rest) >= 2 else None
    if region is None and city in _HUB_REGION and country in (None, "US"):
        return city, _HUB_REGION[city], "US"
    if region is None and city and city.removesuffix(" state") in _US_STATE_NAMES and \
            city.removesuffix(" state") not in ("washington",):
        return None, _US_STATE_NAMES[city.removesuffix(" state")], "US"     # a bare US state ("New York")
    if region is not None and country is None and _region_code(region):
        country = "US"
    return city, region, country


def residence_match(profile: ProfileLike, locations: Sequence[str]) -> bool | None:
    """True: the profile's city + region equal a posting location. False: every posting location is confirmed in
    another region or country. None: not computable (no geocoder, so a nearby city is never decided)."""
    city, region = normalize(str(profile.lookup("contact:city") or "")), str(profile.lookup("contact:region") or "")
    home = country_iso(profile.lookup("country") or "")
    locs = [x for x in locations if str(x or "").strip()]
    if not city or not region or not home or not locs:
        return None
    mine = _region_code(region) if home == "US" else normalize(region)
    verdicts = []
    for loc in locs:
        lc, lr, lco = _loc_region(str(loc))
        if lco is not None and lco != home:
            verdicts.append(False)
            continue
        if lr is None:
            verdicts.append(None)
            continue
        theirs = _region_code(lr) if (lco or home) == "US" else normalize(lr)
        if theirs is None or mine is None:
            verdicts.append(None)
        elif theirs != mine:
            verdicts.append(False)
        else:
            verdicts.append(True if _CITY_ALIAS.get(lc or "", lc) == _CITY_ALIAS.get(city, city) else None)
    if any(v is True for v in verdicts):
        return True
    return False if verdicts and all(v is False for v in verdicts) else None


def _work_pref_ok(text: str, profile: ProfileLike) -> bool:
    """The office / hybrid / on-site commitment an option or label also makes is backed by [preferences]."""
    hyb, ons = _truth(profile.lookup("structured:open_to_hybrid")), _truth(profile.lookup("structured:open_to_onsite"))
    if not _RES_OFFICE.search(text):
        return True
    if _RES_ONSITE.search(text):
        return ons is True
    if _RES_HYBRID.search(text):
        return hyb is True
    return hyb is True or ons is True


def _res_option_class(o: str) -> str:
    """'here' (asserts residence here), 'relocate' (not based here, willing to relocate) or 'other'."""
    n = re.sub(r"\b(?:don|doesn|isn|aren|can|won|didn|wasn|haven) t\b", "not", normalize(o))   # "don't" -> "not"
    m = _RES_HERE.search(n)
    if not m:
        return "other"
    before, after = n[:m.start()], n[m.end():]
    if not has_negator(before):
        return "here" if not has_negator(after) else "other"
    if re.search(r"\brelocat", after) and not re.search(r"\b(?:not|unable|cannot|can not|never|nor|no)\b(?: \w+){0,3} "
                                                        r"relocat|\bunwilling\b", after):
        return "relocate"
    return "other"


def _plain_yes_no(options: Sequence[str]) -> bool:
    """Every option is a bare Yes / No (not "Yes, I'm based in this location ...")."""
    return all(normalize(o) in ("yes", "no") for o in options)


def is_residence_coupled(f: FormField) -> bool:
    """A choice whose options assert residence at the posting location, or a yes/no label that asks it ("Are you
    based in the listed location and ...?"). A conditional ("If you are not local to the area, ...") is not."""
    if f.options and not _plain_yes_no(f.options):
        return any(_res_option_class(o) != "other" for o in f.options)
    n = normalize(f.label)
    m = _RES_HERE.search(n)
    if not m or f.kind not in (FieldKind.boolean, FieldKind.select):
        return False
    before = n[:m.start()]
    return not re.search(r"\b(?:if|unless|when|whether|once)\b", before) and not has_negator(before)


def residence_coupled_value(f: FormField, profile: ProfileLike, locations: Sequence[str],
                            job_country: str | None = None) -> Any | None:
    """ADR-0055: the answer to a residence-coupled field, or None (unresolved). Never claims residence without a
    confirmed city + region match."""
    locs = list(locations)
    home = country_iso(profile.lookup("country") or "")
    match = residence_match(profile, locs)
    if match is None and job_country and home and job_country != home:
        match = False                                    # a confirmed job country other than the home country
    reloc = _truth(profile.lookup("structured:willing_to_relocate"))
    if f.options and not _plain_yes_no(f.options):
        if match is True:
            want = [o for o in f.options if _res_option_class(o) == "here" and _work_pref_ok(normalize(o), profile)]
        elif match is False and reloc is True:
            want = [o for o in f.options if _res_option_class(o) == "relocate" and _work_pref_ok(normalize(o), profile)]
        else:
            return None
        if len(want) != 1:
            return None
        return [want[0]] if f.kind == FieldKind.multiselect else want[0]
    # yes/no label: "Are you based in the listed location and able to ...?" / "... reside within commuting distance OR
    # are you open to relocation ...?"
    n = normalize(f.label)
    either = bool(re.search(r"\bor (?:are|would|will|do|be) you\b|\bor (?:are |be )?(?:open|willing) to relocat", n))
    if either and re.search(r"relocat", n):
        if match is True or reloc is True:
            return "Yes"
        return "No" if match is False and reloc is False else None
    pref = _work_pref_ok(n, profile)
    if match is True and pref:
        return "Yes"
    if match is False:
        return "No"
    return None


# ---- office questions that name the company's office (ADR-0055) -----------------------------------------------
# "Will you be able to work from our office in Union Square, NYC?", "Are you open to working out of our Torrance (LA)
# Office 5 Days a Week?" (options Yes / No / "Yes, but require relocation"). Yes needs the [preferences] fact AND
# either a confirmed residence match or willing_to_relocate; a relocation-qualified Yes is chosen on a confirmed
# mismatch, a plain Yes only on a match.
_OFFICE_Q = re.compile(r"\b(?:our|one of our|the company s)\b(?: \w+){0,4} offices?\b")
_OFFICE_PICK = re.compile(r"\b(?:which|what|preferred|prefer|preference)\b.*\b(?:location|office)s?\b")


def _office_value(f: FormField, ck: str, profile: ProfileLike, locations: Sequence[str],
                  job_country: str | None, gap: Any) -> Any | None:
    n = normalize(f.label)
    key = "open_to_onsite" if _RES_ONSITE.search(n) or ck == "open_to_onsite" else "open_to_hybrid"
    pref = _truth(profile.lookup("structured:" + key))
    if pref is None:
        gap("preferences." + key)
        return None
    home = country_iso(profile.lookup("country") or "")
    match = residence_match(profile, locations)
    if match is None and job_country and home and job_country != home:
        match = False
    reloc = _truth(profile.lookup("structured:willing_to_relocate"))
    plain = f.kind == FieldKind.boolean or not f.options or _plain_yes_no(f.options)
    if plain:
        if pref and (match is True or reloc is True):
            return "Yes"
        return "No" if (pref is False or (match is False and reloc is False)) else None
    yes = [o for o in f.options if tokens(o)[:1] == ["yes"] and not has_negator(re.sub(r"\brequire\w*", "", o))]
    no = [o for o in f.options if tokens(o)[:1] == ["no"]]
    relo = [o for o in yes if re.search(r"relocat", o, re.IGNORECASE)]
    bare = [o for o in yes if o not in relo]
    if pref is False:
        pick = no
    elif match is True:
        pick = bare
    elif match is False and reloc is True:
        pick = relo or bare
    else:
        pick = []
    if len(pick) != 1:
        return None
    return [pick[0]] if f.kind == FieldKind.multiselect else pick[0]


def office_pick_value(f: FormField, profile: ProfileLike, locations: Sequence[str]) -> Any | None:
    """"Which of our offices are you interested in?" (options are places, maybe "Remote"): the office where the owner
    lives; else, when willing to relocate and open to on-site or hybrid work, the posting's primary location."""
    places = [o for o in f.options if not re.search(r"\bremote\b", o, re.IGNORECASE)]
    if len(places) < 2 or not all(_loc_region(o)[0] or _loc_region(o)[1] for o in places):
        return None
    here = [o for o in places if residence_match(profile, [o]) is True]
    pick = here
    if not here:
        reloc = _truth(profile.lookup("structured:willing_to_relocate"))
        office = (_truth(profile.lookup("structured:open_to_onsite")) is True
                  or _truth(profile.lookup("structured:open_to_hybrid")) is True)
        primary = _loc_region(str(locations[0]))[0] if locations else None
        pick = [o for o in places if primary and _loc_region(o)[0] == primary] if reloc is True and office else []
    if len(pick) != 1:
        return None
    return [pick[0]] if f.kind == FieldKind.multiselect else pick[0]


# ---- U.S.-person status (ADR-0055) ---------------------------------------------------------------------------------
# Answered only from the structured fact ([authorization].us_person, else a trusted policy.us_person) and only when every
# word of the label is U.S.-person / export-control definition wording. Anything else stays with the owner.
_USP_VOCAB = frozenset(["a", "an", "the", "is", "are", "am", "i", "you", "your", "of", "or", "and", "for", "as", "by",
                        "under", "in", "to", "with", "which", "what", "following", "best", "describes",
                        "status", "please", "select", "indicate", "confirm", "u", "s", "us", "person", "persons",
                        "united", "states", "america", "citizen", "citizens", "legal", "lawful", "lawfully", "permanent",
                        "resident", "residents", "temporary", "refugee", "refugees", "asylee", "asylees", "asylum",
                        "green", "card", "holder", "holders", "protected", "individual", "individuals", "e", "g",
                        "defined", "definition", "means", "meaning", "within", "purposes", "purpose", "itar", "ear",
                        "export", "control", "controls", "regulations", "regulation", "administration",
                        "international", "traffic", "arms", "laws", "law", "who", "has", "been", "granted", "admitted",
                        "currently", "do", "does", "qualify", "qualifies", "considered", "applicable", "1324b", "8",
                        "c", "section", "yes", "no"])


def us_person_label_ok(label: str) -> bool:
    """ONE question about the candidate's own U.S.-person status: definition sentences allowed, then a single question
    that ends the label. A second question, another person or any other legal topic leaves it to the owner."""
    raw = (label or "").strip()
    _, mark, after = raw.partition("?")
    if mark and after.strip():                       # "...? Have you ever been convicted ...?"
        return False
    words = normalize(raw).split()
    return bool(words) and all(w in _USP_VOCAB or w.isdigit() for w in words)


def _us_person_value(f: FormField, profile: ProfileLike, gap: Any) -> Any | None:
    if not us_person_label_ok(f.label):
        return None
    fact = _truth(profile.lookup("us_person"))
    if fact is None:
        gap("authorization.us_person")
        return None
    if f.kind == FieldKind.boolean or not f.options or _yes_no_options(f.options):
        return _yn(fact) if (f.kind == FieldKind.boolean or f.options) else None
    yes = [o for o in f.options if re.search(r"\bu ?s persons?\b", normalize(o)) and not has_negator(o)
           and not re.search(r"\bnone\b", normalize(o))]
    if fact:
        pick = yes
    else:
        cit = [x.strip() for x in re.split(r"[,;/]| and ", str(profile.lookup("citizenship_country") or "")) if x.strip()]
        isos = [country_iso(x) for x in cit]
        if not isos or any(i is None or i == "US" for i in isos):
            return None
        rxs = [_country_rx(i) for i in isos]

        def names_mine(o: str) -> bool:
            n = normalize(o)
            return any(rx is not None and rx.search(n) for rx in rxs) or any(normalize(c) in n for c in cit)
        others = [o for o in f.options if o not in yes]
        if any(rx is None for rx in rxs):
            return None
        if any(names_mine(o) for o in others):
            return None                                  # an option naming the owner's own country: the owner decides
        else:
            pick = [o for o in others if re.search(r"\bnone of the above\b|\b(?:a |an )?(?:different|another|other) "
                                                    r"country\b", normalize(o))]
    if len(pick) != 1:
        return None
    return [pick[0]] if f.kind == FieldKind.multiselect else pick[0]


def _country_rx(iso: str | None) -> re.Pattern[str] | None:
    pat = COUNTRY_NAMES.get(iso or "")
    return re.compile(rf"\b(?:{pat})\b") if pat else None


def _semantic(f: FormField, ck: str, profile: ProfileLike, company: str, top_languages: int,
              company_aliases: Sequence[str] = (), job_country: str | None = None,
              gaps: list[dict[str, Any]] | None = None) -> tuple[bool, Any]:
    """(handled, value). handled=False -> continue with the generic profile lookup."""
    n = normalize(f.label)
    if ck == "located_in_us":
        return True, _residence_answer(f, profile)
    if ck == "us_citizen":
        if _AUTH_WORDS.search(n):
            return _auth_answer(f, "work_authorized", profile, job_country, gaps, company)
        if re.search(r"permanent resident|green card|national\b", n):
            return True, None
        v = profile.lookup("us_citizen")
        return (False, None) if v is None else (True, v)
    if ck in ("work_authorized", "requires_sponsorship"):
        home = str(profile.lookup("home_country") or "").upper() or None
        if n in ("visa", "visa status") and (job_country is None or job_country != home):
            return True, None                # ADR-0057: a bare "Visa" only on a confirmed home-country job
        return _auth_answer(f, ck, profile, job_country, gaps, company)
    if ck in ("relocation_support", "open_to_hybrid", "open_to_onsite", "willing_to_relocate"):
        if not _YESNO_STYLE.search(n):
            return True, None
        v = profile.lookup(ck)
        if v is None and gaps is not None:                    # ADR-0053: [preferences] fact missing
            gaps.append({"country": None, "fact": "preferences." + _PREF_FIELD.get(ck, ck), "key": f.key,
                         "profile_fact": True})
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
        cur = _CURRENT_EMPLOYEE.match(_mark_all(n, company, company_aliases)) if company else None
        if cur:
            # ADR-0057: "Are you currently an employee of <company> (or a <company> agency)?" is about the CURRENT
            # [[work]] employer only: Yes when it is <company>, else No (no current entry also means No)
            mine = profile.lookup("work:current_company")
            from .adapters import same_company
            if mine and any(same_company(str(mine), x) for x in (company, *company_aliases) if x):
                return True, "Yes"
            if mine is None and profile.lookup("work_history") and _has_current_ambiguity(profile):
                return True, None
            return True, "No"
        worked = bool(company) and bool(profile.lookup("employer:" + normalize(company)))
        if company and re.search(r"\b(?:worked|work|working) with\b", n):
            # ADR-0053 (control): "worked WITH <company>" may mean a vendor or partner relationship. Employment
            # proves Yes; its absence proves nothing, so never a No
            return True, ("Yes" if worked and about_this_company(_AFFILIATE_TAIL.sub("", n), company, company_aliases)
                          else None)
        if company and not about_this_company(f.label, company, company_aliases):   # ADR-0038: government/competitor/other employers
            # ADR-0053: "... <Company> or a <Company> affiliate?" is Yes when <Company> is in the work history; a No
            # would also deny every affiliate, which the profile cannot know, so that case stays with the owner
            if worked and about_this_company(_AFFILIATE_TAIL.sub("", n), company, company_aliases):
                return True, "Yes"
            return True, profile.lookup(n)                            # only the user's own explicit answer
        if not company:
            return True, profile.lookup(n)
        if not worked and not profile.lookup("work_history"):        # ADR-0053: no work history at all: not a "No"
            if gaps is not None:
                gaps.append({"country": None, "fact": "work", "key": f.key, "profile_fact": True})
            return True, None
        return True, "Yes" if worked else "No"
    if ck == "marketing_optin":               # ADR-0050: owner-only, never an automatic No (or Yes)
        return True, None
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
                              r"passport|1324b|debar|lawful|permitted to be employed|public trust|charge|crime|\bdui\b|"
                              r"\bdwi\b|background|screen|\bpr\b|schengen|leave to remain|blue card|legally|"
                              r"working from|work from")
_NEVER_LLM_PLACE = re.compile(r"resid|\blocat|\blive\b|\bliving\b|\bbased\b|relocat|commut|\bon ?site\b|\bin office\b|"
                              r"\bin person\b|hybrid|\bremote\b|time ?zones?\b|continental|contiguous|\bzip\b|postal|"
                              r"\bcity\b|\bstate\b|\bcountry\b|\baddress\b|\btravel")
# extra place wording for SHORT answers only (a textarea essay such as "Where do you see yourself in 5 years?" may still
# go to the writer; a one-line "Which office would you prefer?" never goes to a model)
_NEVER_LLM_PLACE_SHORT = re.compile(r"\boffices?\b|\bmetro\b|\bnear\b|\bwhere\b|\bmoving\b|\bmove\b|\bhq\b|"
                                    r"\bheadquarters\b|\bwork (?:in|from)\b|\barea\b|\bregion\b|\bon campus\b|"
                                    r"\bcome (?:in|to)\b")


# ADR-0040 (final, fail-closed): the short-answer model may only answer a label that FULLY matches one of these strict
# templates (normalized label, anchored). One allowlisted word inside another question never qualifies. Questions
# that would make the model guess a fact about the candidate (years with X, experience with X) are not templates:
# the profile answers them deterministically or the owner does.
_LLM_SAFE_TEMPLATES = [re.compile(p) for p in (
    (r"(?:which|what) (?:tools|technologies|frameworks|libraries|programming languages|languages|databases|platforms)"
     r"(?: do you| have you)?(?: (?:use|used|know|prefer|work with))?"),
    (r"(?:what is your |what are your )?(?:favou?rite|preferred) (?:build tool|tool|programming language|language|"
     r"editor|ide|framework|library|database)"),
    r"how did you (?:hear|find out|learn) about (?:us|this (?:role|job|position|opportunity))",
    r"(?:github|portfolio|website|personal website)(?: url| link| profile)?",
    r"(?:what is your )?notice period",
    r"(?:what is your )?earliest (?:possible )?start date",
)]


def llm_safe_short(f: FormField) -> bool:
    """True only when the whole normalized label matches an allowlisted template and names nothing sensitive."""
    n = normalize(f.label)
    if not n or never_llm(f) or eeo_class(f.label)[0] or _CONSENT.search(f.label):
        return False
    return any(t.fullmatch(n) for t in _LLM_SAFE_TEMPLATES)


def never_llm_legal_place(f: FormField) -> bool:
    """never_llm() without the ADR-0050 contact-permission clause: legal-status and place wording only (ADR-0052)."""
    texts = [normalize(f.label), *(normalize(o) for o in f.options)]
    if any(_NEVER_LLM_LEGAL.search(t) or _NEVER_LLM_PLACE.search(t) for t in texts):
        return True
    return f.kind != FieldKind.textarea and any(_NEVER_LLM_PLACE_SHORT.search(t) for t in texts)


def never_llm(f: FormField) -> bool:
    """True for legal-status and place questions (label or options): deterministic rule or owner only."""
    texts = [normalize(f.label), *(normalize(o) for o in f.options)]
    if any(_NEVER_LLM_LEGAL.search(t) or _NEVER_LLM_PLACE.search(t) for t in texts):
        return True
    choice = f.kind in (FieldKind.boolean, FieldKind.select, FieldKind.multiselect) or bool(f.options)
    if contact_permission_channels(f.label, choice=choice) or _TEXT_OPTIN.search(_strip_exceptions(texts[0])):
        return True                     # ADR-0050: contact-permission questions are owner-only
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
    # ADR-0040: per-country authorization facts a question needed but the profile lacks ({country, fact, key})
    profile_gaps: list[dict[str, Any]] = field(default_factory=list)
    no_gap_keys: set[str] = field(default_factory=set)     # ADR-0040: unconfirmed-country auth fields, never collected


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
    if not f.options and f.kind != FieldKind.boolean:
        # ADR-0040: a free-text EEO box is blank by default. Only a REQUIRED free-text gender field is filled, and only
        # from demographics.gender_if_required (never from `gender`, never when optional).
        if ck == "gender" and f.required and f.kind in (FieldKind.text, FieldKind.unknown):
            g = profile.lookup("gender_if_required")
            return None if g in (None, "") or _is_decline(g) or not demographic_label_ok(f.label, ck) else str(g)
        return None
    v = profile.lookup("gender_if_required") if ck == "gender" and f.required else None
    if v in (None, ""):
        v = profile.lookup(ck)              # profile.demographics only, never free-form answers
    if v in (None, ""):
        return None
    if _is_decline(v):
        if not f.options:
            return None             # a free-text EEO box is left blank (listed as skipped_voluntary when optional)
        got = decline_option(f.options)
    elif not f.options:
        if not demographic_label_ok(f.label, ck):
            return None
        if f.kind == FieldKind.boolean:
            return _truth(v)
        return str(v) if f.kind in (FieldKind.text, FieldKind.unknown) else None
    else:   # the exact option for a plainly-worded question; otherwise declining (never a nearest guess)
        ok_label = demographic_label_ok(f.label, ck)
        detail = profile.lookup("race_detail") if ck == "race" and ok_label else None
        # ADR-0055: [demographics].race_detail (a finer race value) wins when an option matches it exactly; otherwise the
        # race_ethnicity (EEO-1) option. exact_option never takes a broader or narrower option.
        got = ((exact_option(str(detail), f.options) if detail and not _is_decline(detail) else None)
               or (exact_option(str(v), f.options) if ok_label else None) or decline_option(f.options))
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


# ---- messaging / marketing opt-ins (ADR-0050) -------------------------------------------------------
# Fail closed, descoped: every contact / messaging / marketing permission field is OWNER-ONLY. It is never Yes and
# never an automatic No; client answers are refused, no model, flat [answers] or persona answer ever applies, and it
# stays unresolved. The one exception is the owner's own stored [answers] "optin:<channel>" value for every named
# channel. Automatic declines belong to a future safe-default mode, not to this rule.
_OPTIN_CHANNELS: list[tuple[str, re.Pattern[str]]] = [
    ("whatsapp", re.compile(r"\bwhats ?app\w*\b")),
    ("sms", re.compile(r"\bsms\b|\bmms\b|\b(?:mobile|cell(?:ular)?|phone) messag\w*|\btext(?:s|ed|ing)?\b(?! (?:box|field|area|input|below|above|editor)\b)")),
    ("phone", re.compile(r"\b(?:call(?:s|ed|ing)?|phone(?:d|s)?|voicemail)\b")),
    ("email", re.compile(r"\be ?mail(?:s|ed|ing)?\b")),
    ("signal", re.compile(r"\bsignal\b")),
    ("telegram", re.compile(r"\btelegram\b")),
    ("wechat", re.compile(r"\bwe ?chat\b")),
    ("line", re.compile(r"\b(?:on|via|through|by|over) line\b|\bline app\b")),
    ("viber", re.compile(r"\bviber\b")),
    ("imessage", re.compile(r"\bimessage\b|\brcs\b")),
    ("push", re.compile(r"\bpush\b")),
    # a marketing COMMUNICATION only (control round 4); bare "marketing" / "promotion" is a job topic (see "contact")
    ("marketing", re.compile(r"\bmarketing (?:e ?mails?|messages?|communications?|texts?|offers|updates|materials?)\b|"
                             r"\bnewsletters?\b|\bpromotional (?:e ?mails?|messages?|offers|communications?|texts?)\b|"
                             r"\bpromotions? (?:by|via) (?:e ?mail|text|sms)\b|\bmailing list\b|"
                             r"\babout (?:future|other|new) (?:roles|opportunit\w*|openings|positions|jobs)\b|"
                             r"\btalent (?:community|network|pool)\b|\bjob alerts?\b")),
    # generic contact permission with no named channel: stored Yes needs optin:contact
    ("contact", re.compile(r"\b(?:contact(?:ed|ing)?|reach(?:ed|ing)?(?: out)?|messag\w*|updates?|alerts?|"
                           r"notif\w*|reminders?|communicat\w*|keep in touch|stay in touch|hear from|"
                           r"marketing|promotion(?:s|al)?)\b")),       # broad marker only: never auto-answered
]
# ADR-0050 (deny by default): on a choice or boolean field, any channel / contact word that addresses the candidate
# makes the field a messaging opt-in. Only these exits make it something else.
_OPTIN_ADDRESSED = re.compile(r"\b(?:you|your|me|my|i)\b|\bopt(?:ing|ed)? ?in\b|\bconsent\w*|\bagree\w*|\breceiv\w*|"
                              r"\bsubscri\w*|\bsign(?: me)? up\b|\bok(?:ay)?\b|\ballow\w*|\bpermission\b|\benroll\w*")
# a choice/checkbox whose label starts with the channel ("SMS updates", "WhatsApp: may we ...") is addressed too
_OPTIN_LEADING = re.compile(r"^(?:sms|text|e ?mail|whats ?app|phone|push|marketing|newsletter|signal|telegram)\b")
# skills / experience, facts about the past, other people and the company's own process are not opt-ins
# Exceptions REMOVE only the words they account for (control round 3): "WhatsApp experience", "experience with Twilio
# SMS", "call your references", "on call", "phone screen". Whatever remains is checked again, so "Do you have WhatsApp
# experience, and may we message you there?" still asks to contact the candidate.
_W = r"(?: (?!and\b|or\b|but\b|also\b)\w+)"           # one following word inside the same clause
_OPTIN_NOT = re.compile(
    r"(?:\b\w+ ){0,2}\b(?:experience\w*|expertise|skills?|knowledge|proficien\w*|familiar\w*|background in)\b"
    rf"(?: (?:with|in|using|of|on|building|managing|as)\b{_W}{{0,4}})?|"
    rf"\b(?:built|build|building|developed|develop\w*|implement\w*|designed|designing|integrat\w*|deploy\w*|"
    rf"managed|managing|worked (?:on|with)|administer\w*)\b{_W}{{0,3}}|"   # never "using": "text you using WhatsApp"
    r"\b(?:\d+ )?years?(?: of)?\b|\bengineer\w*|\btools?\b|\btechnolog\w*|\bplatforms?\b|\bapis?\b|\bgateways?\b|"
    r"\btwilio\b|"
    rf"\bhave you (?:ever |previously |already )?(?:been )?(?:contact|call|email|messag|text|reach)\w*"
    rf"(?: by{_W}{{1,4}})?(?: before)?|"
    rf"\b(?:did|has|have) (?:anyone|someone|a recruiter|we|our)\b{_W}{{0,4}}|"
    r"\b(?:your |the |my |our )?(?:current |previous |former |past )?(?:references?|referees?|employers?|managers?|"
    r"supervisors?|emergency contacts?|next of kin)\b|"
    r"\bon ?call\b|\bpager\w*|\bphone (?:interview|screen)\w*|\bvideo (?:call|interview)\w*|\bavailab\w*")


def _names_channel(f: FormField) -> bool:
    """A choice/boolean field whose raw label names any contact channel (before exceptions are removed)."""
    choice = f.kind in (FieldKind.boolean, FieldKind.select, FieldKind.multiselect) or bool(f.options)
    n = normalize(f.label)
    return choice and any(rx.search(n) for c, rx in _OPTIN_CHANNELS if c != "contact")


def _strip_exceptions(n: str) -> str:
    return re.sub(r"\s+", " ", _OPTIN_NOT.sub(" ", n)).strip()


_OPTOUT = re.compile(r"\bopt(?:ing|ed)? ?out\b|\bunsubscri\w*|\bstop receiving\b|\b(?:do not|don t|never) "
                     r"(?:want|wish) to (?:receive|be contacted)\b")


_OPTIN_BUNDLED = re.compile(r"acknowledg|privacy|gdpr|attest|certif|\bterms\b|conditions|polic(?:y|ies)|\btrue\b|"
                            r"accura|arbitrat|background|record(?:ed|ing)?\b|transcri")
_TEXT_OPTIN = re.compile(r"\bopt(?:ing|ed)? ?in\b|\bconsent\w*|\bsubscri\w*|\bsign(?: me)? up\b|\bpermission\b|"
                         r"\b(?:may|can) we (?:contact|text|call|email|message|reach)\b")


def contact_permission_channels(label: str, choice: bool = False) -> list[str]:
    """ADR-0050: the channels a label asks permission to contact the candidate on ([] when it is not such a
    question). Shared by the mapper's opt-in rule and the engine, which refuses client answers for these fields."""
    n = _strip_exceptions(normalize(label))
    chans = [c for c, rx in _OPTIN_CHANNELS if rx.search(n)]
    if not chans or not (_OPTIN_ADDRESSED.search(n) or (choice and _OPTIN_LEADING.search(n))):
        return []
    return [c for c in chans if c != "contact"] or ["contact"]


def optin_channels(f: FormField) -> list[str] | None:
    """The channels a choice/boolean field asks permission to contact the candidate on, or None when it is not a
    contact-permission question. [] = an opt-in the owner must answer (opt-out wording, bundled consent, free text).
    A text field naming a channel ("WhatsApp number", "Your email") is not an opt-in."""
    n = _strip_exceptions(normalize(f.label))      # exceptions remove only the words they cover
    chans = [c for c, rx in _OPTIN_CHANNELS if rx.search(n)]
    if not chans:
        return None                     # "experience with Twilio SMS", "built SMS gateways", "call your references"
    choice = f.kind in (FieldKind.boolean, FieldKind.select, FieldKind.multiselect) or bool(f.options)
    if not choice:
        return [] if _TEXT_OPTIN.search(n) else None
    shared = contact_permission_channels(f.label, choice=True)
    if not shared:
        return None
    if is_auth_question(f) or eeo_class(f.label)[0] or canonical_key(f.label) in _OPTIN_OTHER_KEYS:
        # a question with its own rule; if another clause still asks permission to contact, the owner decides
        return [] if _PERMISSION_ASK.search(n) else None
    if _OPTOUT.search(n) or _OPTIN_BUNDLED.search(n):
        return []                       # opt-out wording, or bundled with a notice/terms/attestation: the owner decides
    return shared


_OPTIN_OTHER_KEYS = {"knows_employee", "prior_employee", "referral_source", "relocation_support", "willing_to_relocate",
                     "open_to_hybrid", "open_to_onsite", "located_in_us", "over_18", "start_date", "years_experience",
                     "languages_known", "email", "phone", "first_name", "last_name", "full_name", "preferred_name",
                     "linkedin", "github", "website", "location", "country", "zip"}


def _no_option(options: Sequence[str]) -> str | None:
    hits = [o for o in options if tokens(o)[:1] == ["no"]] or \
        [o for o in options if _OPTOUT.search(normalize(o)) or re.search(r"\b(?:do not|don t) (?:want|wish|opt)\b",
                                                                          normalize(o))]
    return hits[0] if len(hits) == 1 else None


# ADR-0050: a stored optin:<channel> value is used only for a NARROW permission question (delivery verb, named
# messaging channel or marketing communication, and a contact as the thing delivered). The broad classifier
# (contact_permission_channels) only marks a field owner-only: "Are you comfortable making cold calls?" is never
# answered, not even from a stored optin:phone.
_PERMISSION_ASK = re.compile(
    # control round 4: every alternative names a delivery (receive / be contacted / text you ...); a bare
    # "would you like", "consent" or "permission" never counts
    r"\bopt(?:ing|ed)? ?in(?: to| for|$)|\bsubscri\w* to\b|\bsign (?:me )?up (?:for|to)\b|"
    r"\b(?:consent\w*|agree\w*) to (?:receive|receiving|be (?:contacted|texted|called|emailed|messaged|sent))\b|"
    r"\bpermission to (?:send|contact|text|call|email|message|reach)\w* (?:you|your|me)\b|"
    r"\b(?:may|can|could) (?:we|our (?:team|recruiters?)|recruiters?|\w+ recruiting) (?:send|contact|text|call|email|"
    r"message|reach|whats ?app)\w*(?: or (?:contact|text|call|email|message)\w*)? (?:you|your)\b|"
    r"\bok(?:ay)? to (?:text|call|email|contact|message|whats ?app)\w* (?:you|your)\b|"
    r"\b(?:would|d) (?:you |i )?like to (?:receive|get|be sent|hear)\b|"
    r"\bjoin (?:our|the|\w+(?: \w+)? s) (?:talent (?:community|network|pool)|mailing list|newsletter)\b")
# channels that may be auto-answered; bare phone/call, email, push and generic contact words never are
_AUTO_CHANNELS = frozenset({"whatsapp", "sms", "signal", "telegram", "wechat", "line", "viber", "imessage",
                            "marketing"})


# ---- the single-clause permission template (control round 6; shared with the autonomy-level PR) -----------------
# A stored optin:<channel> is applied ONLY when the whole label is one permission clause. A second clause ("Have you
# been arrested, and may we text you?", "May we text you? Are you a US citizen?") or any legal, attestation, EEO or
# third-party term anywhere rejects it, and the field stays unresolved.
_SECOND_CLAUSE = re.compile(r"\band\b|\bor\b|;|,|\?\s*\S|\.\s+\S|!\s*\S", re.IGNORECASE)
_SINGLE_ASK_BLOCK = re.compile(
    r"arrest|criminal|convict|felon|citizen|authori[sz]|sponsor|visa|\b18\b|\bage\b|certif|attest|\btrue\b|accura|"
    r"background|drug|gender|\brace\b|ethnic|veteran|disab|referen|referee|manager|employer|third|emergency|\bkin\b|"
    r"terms|arbitrat|privacy|record(?:ed|ing)?\b|relocat|salary|clearance|export", re.IGNORECASE)
_ASK_LEAD = r"(?:(?:do you|would you like to|i would like to|i d like to|i|yes)\s)?"
_ASK_VERB = (r"(?:opt in (?:to|for)(?: receive| receiving)?|subscribe to|sign (?:me )?up (?:for|to)|"
             r"(?:consent|agree) to (?:receive|receiving|be (?:contacted|texted|called|emailed|messaged))|"
             r"would you like to (?:receive|get)|"
             r"(?:may|can|could) we (?:send|contact|text|call|email|message|reach|whats ?app) (?:you|your)|"
             r"(?:is it )?ok(?:ay)? to (?:text|call|email|contact|message|whats ?app) (?:you|your))")
_ASK_TAIL = (r"(?:\s(?:about (?:this|my|your) application|about (?:new|future) (?:roles|jobs|opportunities)|"
             r"from (?:us|[a-z0-9 ]{1,40})))?")
_ASK_OBJECT_WORDS = frozenset(["a", "the", "our", "your", "my", "via", "by", "on", "over", "through", "mobile", "phone",
                               "cell", "number", "messages", "message", "updates", "update", "alerts", "alert",
                               "notifications", "notification", "texts", "text", "emails", "email", "e", "mail",
                               "communications", "newsletter", "newsletters", "marketing", "promotional", "offers",
                               "recruiting", "recruiter", "job", "sms", "mms", "whatsapp", "whats", "app", "signal",
                               "telegram", "wechat", "line", "viber", "imessage", "reminders", "talent", "community",
                               "mailing", "list"])
_ASK_RX = re.compile(rf"^{_ASK_LEAD}{_ASK_VERB}(?P<obj>(?:\s[a-z0-9]+){{0,5}}?){_ASK_TAIL}$")


def is_single_permission_ask(label: str, company: str = "") -> list[str] | None:
    """The channels of a label that is exactly ONE permission-to-contact clause (lead-in, permission verb, channel,
    optional short tail), else None. Shared with the autonomy-level `safe_default` mode."""
    raw = (label or "").strip()
    body = re.sub(r"[?.!]+\s*$", "", raw)          # one trailing ? or . is allowed
    if not body or _SECOND_CLAUSE.search(body) or _SINGLE_ASK_BLOCK.search(raw):
        return None
    n = normalize(body)
    m = _ASK_RX.match(n)
    if not m:
        return None
    tail = n[m.end("obj"):].strip()
    if tail.startswith("from ") and tail[5:] not in ("us", "recruiting"):
        allowed = set(normalize(company).split()) | {"recruiting", "recruiters", "team", "talent", "hiring", "s"}
        if not company or not set(tail[5:].split()) <= allowed:
            return None
    if not all(w in _ASK_OBJECT_WORDS for w in m.group("obj").split()):
        return None
    chans = [c for c, rx in _OPTIN_CHANNELS if rx.search(n) and c != "contact"]
    return chans if _AUTO_CHANNELS & set(chans) else None


def optin_value(f: FormField, chans: list[str], profile: ProfileLike, company: str = "") -> Any | None:
    """ADR-0050 (descoped): never an automatic answer. Only the owner's own stored `optin:<channel>` value, set for
    EVERY named channel, answers a label that is exactly one permission clause (is_single_permission_ask)."""
    single = is_single_permission_ask(f.label, company)
    if not single:
        return None
    chans = single
    stored = [_truth(profile.lookup(f"optin:{c}")) for c in chans]
    if any(v is None for v in stored):
        return None                     # no automatic No: the owner (or a later safe-default mode) decides
    want = all(stored)
    if f.kind == FieldKind.boolean:
        return want
    if not f.options:
        return None
    pick = (next((o for o in f.options if tokens(o)[:1] == ["yes"]), None) if want else _no_option(f.options))
    if pick is None:
        return None
    return [pick] if f.kind == FieldKind.multiselect else pick


# ---- flat [answers] and authorization (ADR-0050) -----------------------------------------------------------
# Since ADR-0040 a job outside the home country takes only [authorization.<CC>] facts or a stored
# authorization.<CC>.* answer. A flat [answers] pattern ("now or in the future", "require sponsorship") states a
# home-country fact, so it may answer an authorization/sponsorship question only on a confirmed home-country job.
_AUTH_TOPIC = re.compile(r"sponsor|\bvisas?\b|authori[sz]|eligib\w* to work|right to work|work permit|work rights|"
                         r"permitted to work|allowed to work|legally (?:able|entitled) to work|immigra|"
                         r"\bh ?1 ?b\b|\bopt\b(?! (?:me |us )?(?:in|into|out)\b)|\bcpt\b|\bead\b|leave to remain|blue card")


def is_auth_question(f: FormField) -> bool:
    return any(_AUTH_TOPIC.search(normalize(t)) for t in (f.label, *f.options))


def flat_auth_allowed(f: FormField, profile: ProfileLike, job_country: str | None) -> bool:
    """True only when the question's country (the one it names, else the job's confirmed country) is the home country."""
    named = named_countries(f.label)
    if len(named) > 1 or (named and named[0] is None):
        return False
    country = named[0] if named else job_country
    home = str(profile.lookup("home_country") or "").upper() or None
    return country is not None and country == home


_FACT_RULE_KEYS = PROFILE_FACT_KEYS | {"prior_employee", "willing_to_relocate", "relocation_support", "open_to_hybrid",
                                      "open_to_onsite", "located_in_us", "work_authorized", "requires_sponsorship",
                                      "us_person"}


def fact_rule_field(f: FormField, company: str = "", aliases: Sequence[str] = ()) -> bool:
    """ADR-0053: a field the deterministic profile-fact rules own (employer, title, prior employment, residence,
    preferences, authorization). Left unresolved by those rules, it goes to the owner: the autonomy layer
    (ADR-0052) never assumes or infers it."""
    return (canonical_key(f.label) in _FACT_RULE_KEYS or is_auth_question(f) or is_residence_coupled(f)
            or (bool(company) and about_this_company(f.label, company, aliases)))


def map_fields(fields: list[FormField], profile: ProfileLike, *, referral_preference: list[str] | None = None,
               min_score: float = 0.78, company: str = "", top_languages: int = 3,
               stop_phrases: Sequence[str] = (), company_aliases: Sequence[str] = (), persona: Any = None,
               job_in_us: bool = False, job_country: str | None = None,
               job_locations: Sequence[str] = ()) -> MapResult:
    """`job_country`: the job's confirmed ISO-2 country (see job_country()); `job_in_us=True` is the old alias for "US".
    `job_locations`: the posting's location strings, for residence-coupled options (ADR-0055)."""
    if job_country is None and job_in_us:
        job_country = "US"
    pref = DEFAULT_REFERRAL_PREFERENCE if referral_preference is None else referral_preference
    res = MapResult()
    eeo_keys: set[str] = set()
    gaps: list[dict[str, Any]] = []
    originals: dict[str, FormField] = {}          # ADR-0055: fields mapped with residence options removed

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
        chans = optin_channels(f)
        if chans is not None:            # ADR-0050: messaging/marketing opt-in, before every answer source
            ov = optin_value(f, chans, profile, company)
            if ov is not None:            # the owner's own optin:<channel> answer
                res.answers.append(Answer(key=f.key, value=ov, source="rule:messaging_optin"))
            else:
                res.unresolved.append(f)  # owner-only: never Yes, never an automatic No, no flat/persona/LLM
            continue
        if is_residence_coupled(f):          # ADR-0055 (#85): residence + hybrid/relocation, before any preference rule
            rv = residence_coupled_value(f, profile, job_locations, job_country)
            if rv is not None or not (f.options and not _plain_yes_no(f.options)):
                ok(f, rv if rv is None or isinstance(rv, list) else _coerce(f, rv, min_score), "rule:residence_here")
                continue
            # not decided: the other rules may still pick an option that states no residence ("I am open to
            # relocating"), never one that claims or denies residence here
            rest_opts = [o for o in f.options if _res_option_class(o) == "other"]
            if not rest_opts:
                res.unresolved.append(f)
                continue
            originals[f.key] = f
            f = f.model_copy(update={"options": rest_opts})
        ck = canonical_key(f.label)
        if ck == "us_person":                # ADR-0055: structured U.S.-person fact only, strict wording
            def _usp_gap(fact: str, key: str = f.key) -> None:
                gaps.append({"country": None, "fact": fact, "key": key, "profile_fact": True})
            uv = _us_person_value(f, profile, _usp_gap)       # never an [answers] pattern (control, final round)
            ok(f, uv if uv is None or isinstance(uv, list) else _coerce(f, uv, min_score), "rule:us_person")
            continue
        if ck in ("open_to_onsite", "open_to_hybrid") and _OFFICE_Q.search(normalize(f.label)):
            def _off_gap(fact: str, key: str = f.key) -> None:
                gaps.append({"country": None, "fact": fact, "key": key, "profile_fact": True})
            ov = _office_value(f, ck, profile, job_locations, job_country, _off_gap)   # ADR-0055
            ok(f, ov if ov is None or isinstance(ov, list) else _coerce(f, ov, min_score), f"rule:{ck}")
            continue
        if f.options and _OFFICE_PICK.search(normalize(f.label)) and re.search(r"\boffices?\b", normalize(f.label)):
            pv = office_pick_value(f, profile, job_locations)
            if pv is not None:
                res.answers.append(Answer(key=f.key, value=pv, source="rule:office_location"))
                continue
        if (ck is None and company and not is_auth_question(f)
                and about_this_company(f.label, company, company_aliases)):
            ck = "prior_employee"        # ADR-0053: "Have you worked with <posting company>?" is work history, not a skill
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
        if ck in PROFILE_FACT_KEYS:          # ADR-0053: [contact] / [[work]] facts only, never a flat or persona value
            pv = _profile_fact(f, ck, profile, gaps)
            if ck in ("located_near", "located_in_place") and pv is None:
                pv = profile.lookup(normalize(f.label))   # only the owner's own explicit answer for that wording
            c = pv if (pv is None or isinstance(pv, list)) else _coerce(f, pv, min_score)
            ok(f, c, f"rule:{ck}")
            continue
        if ck in _SEMANTIC_KEYS:
            handled, sv = _semantic(f, ck, profile, company, top_languages, company_aliases, job_country, gaps)
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
        if is_auth_question(f):          # ADR-0050: only structured per-country facts (above) answer these
            res.unresolved.append(f)
            if not flat_auth_allowed(f, profile, job_country):
                gaps.append({"country": None, "fact": None, "key": f.key})
            continue
        if _names_channel(f):            # ADR-0050: a choice field naming a contact channel never takes a flat,
            res.unresolved.append(f)     # persona or alias-phrase answer (only optin:<channel>, via the rule above)
            continue
        value: Any | None = profile.lookup(ck) if ck else None
        persona_src = False
        if value is None and ck is None and persona is not None:   # ADR-0047: after every sensitive guard above
            value = persona.claim(f.label, ck, stop_phrases)
            persona_src = value is not None
        if value is None:
            value = profile.lookup(normalize(f.label))
        coerced = _coerce(f, value, min_score) if value is not None else None
        if (coerced is not None and ck is None and not f.options and f.kind in (FieldKind.text, FieldKind.textarea)
                and normalize(str(coerced)) in ("yes", "no") and not _YESNO_STYLE.search(normalize(f.label))):
            coerced = None                       # a saved Yes/No never goes into a free-text question
        if coerced is None or coerced == "":
            res.unresolved.append(f)
        else:
            res.answers.append(Answer(key=f.key, value=coerced, source="persona:answer" if persona_src else "profile" if ck is None else f"rule:{ck}"))
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
    res.unresolved = [originals.get(x.key, x) for x in res.unresolved]      # the owner sees every option
    answered = {a.key for a in res.answers}
    res.profile_gaps = list({(g["country"], g["fact"]): g for g in gaps
                             if g["key"] not in answered and (g["country"] or g.get("profile_fact"))}.values())
    # authorization/sponsorship fields whose country is not confirmed: never a gap under any reusable key
    res.no_gap_keys = {g["key"] for g in gaps if not g["country"] and not g.get("profile_fact")
                       and g["key"] not in answered}
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

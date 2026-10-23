"""Configurable autonomy level (ADR-0052): what the agent may assume when the owner is not available.

Levels (settings `answers.autonomy`, trusted user config only; see config._RELAXES):
  strict        unresolved fields go to the owner (the behaviour before ADR-0052)
  safe_default  a deterministic SAFE answer where one exists:
                  - contact / marketing permission asks: the declining option (No, or unticked)
                  - "how did you hear about us": the configured default
                  - EEO / demographic: "decline to self-identify" when offered, else blank
                  - optional free text: blank (as before)
                everything else stays unresolved
  infer         safe_default plus an ALLOWLIST of skill / experience choice questions about a tool, technology,
                language, domain or skill ("experience with X?", "proficient in X?"), answered by the LLM from the
                resume and profile with a conservative prompt: Yes only when the facts clearly support it, otherwise
                the honest No or the lowest option. Willingness, ability, availability, location, consent and terms
                questions are never inferred

Hard limits apply at EVERY level and are checked first: authorization, sponsorship, citizenship, visa; identity
(E-Verify, I-9); criminal history, background checks, drug tests; screening (polygraph, psychological, physical);
accommodation; recording / social media; employment terms (trial, unpaid, contract, government, firearms); attestations and certifications; legal agreements; salary; start date, relocation and
location facts. Those fields stay structured-facts-only or unresolved.

Every assumed answer carries `assumed=True` and its rule or rationale (plan, evidence, run report). Assumptions are
counted per normalized question kind (`assumed.*` keys); after `answers.promote_after` of one kind the question is added
to the profile gaps so the owner is asked once. A stored owner answer for that key replaces the assumption.
"""
from __future__ import annotations

import json
import re
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from ..core.models import Answer, FieldKind, FormField
from .mapper import (
    _DEMOGRAPHIC_LABEL,
    _SECOND_CLAUSE,
    _SINGLE_ASK_BLOCK,
    DEMOGRAPHIC_KEYS,
    best_option,
    canonical_key,
    contact_permission_channels,
    decline_option,
    eeo_class,
    eeo_option_ok,
    is_auth_question,
    is_single_permission_ask,
    named_countries,
    never_llm_legal_place,
    normalize,
    question_countries,
    tokens,
)
from .salary import is_salary_field

LEVELS = ("strict", "safe_default", "infer")

# ---- hard limits ------------------------------------------------------------------------------------------------
_HARD: list[tuple[str, re.Pattern[str]]] = [
    ("authorization", re.compile(r"citizen|nationalit|\bvisas?\b|immigra|green card|permanent resid|sponsor|"
                                 r"authori[sz]|work permit|right to work|eligible to work|export|\bitar\b|clearance")),
    ("criminal_background", re.compile(r"criminal|convict|felon|arrest|misdemeano|\bcharged?\b|background|"
                                       r"\bdrug|substance|toxicolog|fingerprint|credit check|consumer report|\bfcra\b")),
    ("attestation", re.compile(r"certif|attest|true and (?:complete|correct|accurate)|accurate and complete|"
                               r"complete and (?:accurate|true|correct)|best of my knowledge|under penalty|signature|"
                               r"\be ?sign|\binitials\b|\bi (?:hereby )?(?:declare|confirm|acknowledge|understand)\b|"
                               r"acknowledg|by (?:checking|selecting|clicking|submitting)")),
    ("legal_agreement", re.compile(r"arbitrat|\bnda\b|non ?disclosure|non ?compete|non ?solicit|confidentiality|"
                                   r"agreement|\bwaive|class action|terms (?:of|and)|conditions of|at will|"
                                   r"\bi (?:agree|accept)\b")),
    ("salary", re.compile(r"salary|compensation|\bpay\b|\bwages?\b|hourly rate|\brate expectation|\bbonus\b|\bequity\b")),
    ("start_date", re.compile(r"\bstart\b|notice period|available to (?:begin|join)|when can you|join date|\bbegin\b")),
    ("relocation_location", re.compile(r"relocat|commut|on ?site|in office|in person|hybrid|\bremote\b|"
                                       r"\bvisit|\btravel|\boffices?\b|\bmetro\b|time ?zone|\bwhere\b|client site|"
                                       r"customer site|abroad|overseas|\binternational\b")),
    # control review of #75: identity, screening, medical / ADA, privacy, unpaid and employment terms
    ("identity", re.compile(r"e ?verify|\bi ?9\b|proof of identity|\bidentity\b|\bidentification\b|\bpassport\b|"
                            r"\bssn\b|social security")),
    ("screening", re.compile(r"polygraph|lie detector|psycholog|personality test|physical (?:exam|test|evaluation|"
                             r"assessment|requirements?)|\bphysical\b|\bmedical\b|\bvaccin|\bscreening\b")),
    ("disability_accommodation", re.compile(r"accommodat|essential functions|\bdisab")),
    ("privacy_recording", re.compile(r"record(?:ed|ing|ings)?\b|\bcamera\b|\bon video\b|video (?:interview|call)s?|"
                                     r"social media|\bmonitored\b|keystroke|surveillance|\bbiometric|"
                                     r"(?:your|personal) data\b")),
    ("employment_terms", re.compile(r"trial period|\bunpaid\b|for free|without pay|\bvolunteer|\bcontract\w*|"
                                    r"\bfreelanc|\b1099\b|\bw ?2\b|probation|government|\bfederal\b|\bmilitary\b|"
                                    r"firearm|weapon|\bgun\b|\bshifts?\b|weekends?|overtime|\bnights?\b|on ?call\b")),
]
# plain opt-in wording a clean contact-permission ask may carry ("I agree to receive ...", "By checking this box")
_CONSENT_WORDS = re.compile(r"\bi (?:agree|accept)\b|\bby (?:checking|selecting|clicking|ticking)\b")
_PLACE_KEYS = {"start_date", "willing_to_relocate", "relocation_support", "location", "located_in_us", "open_to_hybrid",
               "open_to_onsite", "country", "zip", "salary_expectation", "work_authorized", "requires_sponsorship",
               "us_citizen", "citizenship", "citizenship_countries", "over_18"}


def hard_limit(f: FormField, *, contact: bool = False) -> str | None:
    """The hard-limit category of a field (never auto-decided at any level), or None.
    `contact=True`: a clean contact-permission ask (ADR-0050 classifier) may carry plain "I agree" wording; only that
    attestation/agreement wording is ignored, every other category still applies."""
    texts = [normalize(t) for t in (f.label, *f.options)]
    if is_auth_question(f):
        return "authorization"
    if is_salary_field(f):
        return "salary"
    ck = canonical_key(f.label)
    if ck in _PLACE_KEYS:
        return "start_date" if ck == "start_date" else "salary" if ck == "salary_expectation" else \
            "authorization" if ck in ("work_authorized", "requires_sponsorship", "us_citizen", "citizenship",
                                      "citizenship_countries", "over_18") else "relocation_location"
    for name, rx in _HARD:
        if any(rx.search(_CONSENT_WORDS.sub(" ", t) if contact else t) for t in texts):
            return name
    if any(named_countries(t) or question_countries(t)[1] for t in (f.label, *f.options)):
        return "relocation_location"                 # a country or region named anywhere: a place fact
    if never_llm_legal_place(f):                     # ADR-0040 legal-status / place wording (anchored list)
        return "relocation_location" if not _HARD[0][1].search(texts[0]) else "authorization"
    return None


# ---- safe answers ---------------------------------------------------------------------------------------------
# a request for permission to contact the candidate (owned here; the ADR-0050 classifier only names the channels)
_ASKS = re.compile(
    r"\b(?:may|can|could|shall) (?:we|i|our \w+|\w+ recruit\w*|recruiters?|the \w+ team)\b.*\b(?:contact|text|call|"
    r"e ?mail|message|reach|send|notify|whats ?app|keep|add)\w*\b|\bopt ?in\b|\boptin\b|\bconsent\w*|\bsubscri\w*|"
    r"\bsign (?:me |you )?up\b|\bpermission\b|\bwould you like\b|\bdo you (?:want|wish) to (?:receive|get|hear|be "
    r"(?:contacted|notified|added))\b|\b(?:agree|ok(?:ay)?|happy|fine) to (?:receive|be contacted|be (?:texted|called|"
    r"emailed|messaged)|hear)\b|\breceiv\w*\b.*\b(?:updates?|news|e ?mails?|messages?|texts?|alerts?|notifications?|"
    r"newsletters?|marketing|communications?|offers|information)\b|\bkeep (?:me|you) (?:informed|updated|posted)\b|"
    r"\bstay in touch\b|\bjoin (?:our|the)\b.*\b(?:talent|community|network|pool|mailing list|newsletter)\b")
_NEG_OPTION = re.compile(r"^no\b|\b(?:do not|don t|dont|not interested|no thanks|no thank you|opt (?:me )?out|decline|"
                         r"unsubscribe|never)\b")
_NONE_OPTION = re.compile(r"^(?:none|no thanks|no thank you|none of (?:the|these) ?(?:above)?)$")


def _choice(f: FormField) -> bool:
    return f.kind in (FieldKind.boolean, FieldKind.select, FieldKind.multiselect) or bool(f.options)


# A GENUINE permission ask. The single source of truth is ADR-0050's is_single_permission_ask (one anchored clause,
# a named messaging/marketing channel, no second clause, no legal/attestation/third-party term). A few common asks it
# does not accept (generic channels, "join our talent community") are listed here: anchored whole-label, single
# clause, and rejected by the SAME second-clause and blocked-term checks. The ADR-0050 template is never widened.
_V = r"(?:text|call|e ?mail|contact|message|whats ?app|reach)"
_ABOUT = (r"(?: (?:about|regarding) (?:interview scheduling|scheduling (?:an |your )?interviews?|your application|"
          r"this application|this role|this position|(?:new|future|other|similar|relevant) (?:roles|jobs|"
          r"opportunities|openings|positions)|updates)| with updates)?")
_EXTRA_ASKS = [re.compile(p) for p in (
    rf"(?:can|may|could) we {_V}(?: or {_V})? you(?: (?:by|via|on|at) (?:text|sms|phone|e ?mail))?{_ABOUT}",
    rf"(?:is it )?ok(?:ay)? to {_V} you{_ABOUT}",
    (r"(?:(?:do you want|would you like) to |i (?:would like|want) to )?join (?:our|the) (?:talent (?:community|network|"
     r"pool)|mailing list|newsletter)"),
    (rf"(?:do you want|would you like) to (?:receive|get) (?:e ?mail |text |sms |job )?(?:updates|alerts|notifications)"
     rf"{_ABOUT}"),
)]
# "email or contact you": a disjunction of contact verbs inside the ONE clause, not a second clause
_VERB_OR = re.compile(rf"\b{_V} or {_V} you\b", re.IGNORECASE)


def contact_channels(f: FormField, company: str = "") -> list[str] | None:
    """Channels of a GENUINE contact / marketing permission ask, else None. Compound labels ("Have you been arrested,
    and may we text you?") are never asks."""
    if not _choice(f):
        return None
    chans = is_single_permission_ask(f.label, company)
    if chans:
        return list(chans)
    raw = (f.label or "").strip()
    body = re.sub(r"[?.!]+\s*$", "", raw)
    if not body or _SECOND_CLAUSE.search(_VERB_OR.sub("verb you", body)) or _SINGLE_ASK_BLOCK.search(raw):
        return None
    n = normalize(body)
    if any(t.fullmatch(n) for t in _EXTRA_ASKS):
        return contact_permission_channels(f.label, choice=True) or ["contact"]
    return None


def _unique(opts: list[str]) -> str | None:
    return opts[0] if len(opts) == 1 else None


def decline_contact(f: FormField) -> Any | None:
    """No / unticked for a contact-permission ask; None when the field offers no clean declining choice."""
    if f.kind == FieldKind.boolean and not f.options:
        return False
    if f.kind == FieldKind.multiselect:
        got = _unique([o for o in f.options if _NONE_OPTION.match(normalize(o))])
        return [got] if got else None
    return _unique([o for o in f.options if _NEG_OPTION.search(normalize(o))])


def eeo_field(f: FormField) -> bool:
    return eeo_class(f.label)[0] or canonical_key(f.label) in DEMOGRAPHIC_KEYS or bool(_DEMOGRAPHIC_LABEL.search(f.label))


def eeo_decline(f: FormField) -> Any | None:
    """The "decline to self-identify" option when the field offers one; otherwise None (blank)."""
    if not f.options:
        return None
    got = decline_option(f.options)
    if got is None or not eeo_option_ok(got, eeo_class(f.label)[1], f.options):
        return None
    return [got] if f.kind == FieldKind.multiselect else got


_REFERRED = re.compile(r"referr|employee|friend|colleague|coworker|recruiter reached", re.IGNORECASE)


def referral_default(f: FormField, prefs: Sequence[str]) -> Any | None:
    """The configured "how did you hear" default: the first preference that is an option (never a referral option),
    else a plain "Other" option; for free text the first preference."""
    prefs = [p for p in prefs if p]
    if not f.options:
        return prefs[0] if prefs and f.kind in (FieldKind.text, FieldKind.unknown) else None
    pool = [o for o in f.options if not _REFERRED.search(o)]
    for p in prefs:
        got = best_option(p, pool, cutoff=0.78)
        if got:
            return [got] if f.kind == FieldKind.multiselect else got
    other = _unique([o for o in pool if re.match(r"^other\b", normalize(o))])
    return None if other is None else [other] if f.kind == FieldKind.multiselect else other


# ---- infer: skill / experience questions (allowlist) ----------------------------------------------------------
# ALLOWLIST (control review of #75): only skill / experience questions whose object X is a tool, technology, language,
# domain or skill. Willingness, ability, availability, location, consent and terms questions are never inferred.
_X = r"(?P<x>[a-z0-9][a-z0-9 ]{0,48})"
_CAP = [re.compile(p) for p in (
    (r"(?:do|does) you have (?:any )?(?:prior |previous |professional |hands on |practical |working |direct )?"
     rf"(?:experience|familiarity|exposure|proficiency|expertise) (?:with|in|using) {_X}"),
    rf"(?:are you|do you consider yourself) (?:proficient|experienced|familiar|skilled) (?:with|in|at|using) {_X}",
    rf"(?:are you|how) comfortable (?:are you )?using {_X}",
    rf"how (?:proficient|familiar|experienced) are you (?:with|in|using) {_X}",
    rf"have you (?:ever |previously )?(?:used|worked with|programmed in|developed (?:with|in)|built (?:with|in)) {_X}",
    (r"(?:do you have )?(?:at least )?(?:\d+ )?(?:or more )?(?:years? of )(?:professional |hands on )?experience "
     rf"(?:with|in|using) {_X}"),
    rf"how many years of (?:professional |hands on )?experience do you have (?:with|in|using) {_X}",
    (r"(?:please )?(?:rate your|what is your(?: level of)?) (?:proficiency|experience|familiarity|skill level|expertise)"
     rf"(?: level)? (?:with|in|using) {_X}"),
)]
# X must be a short noun phrase naming a tool / technology / skill: no person, place, process, term or activity words
_X_DENY = re.compile(
    r"\b(?:be|being|been|work|working|works|travel\w*|relocat\w*|handl\w*|sign\w*|shar\w*|submit\w*|undergo\w*|"
    r"pass\w*|tak\w*|provid\w*|report\w*|perform\w*|commut\w*|record\w*|camera|free|unpaid|trial|contract\w*|"
    r"government|firearm\w*|weapon\w*|client|clients|customer|customers|site|sites|people|team|teams|manag\w*|"
    r"you|your|our|us|me|my|this|that|these|role|position|job|company|employer|employers|cold|call|calls|calling|"
    r"phone|sales|selling|lift\w*|driv\w*|licen[cs]\w*|vehicle\w*|shift\w*|weekend\w*|night\w*|hours|overtime|"
    r"remote|office|onsite|site|abroad|overseas|europe|country|countries|state|city|move|moving|any|other|such|"
    r"environment|setting|conditions?|pressure|deadlines?|stress|fast|paced|ambiguity|change)\b")
# commitments and conditions are not capabilities (defence in depth on the whole label)
_INFER_EXCLUDE = re.compile(r"\bweekends?\b|\bshifts?\b|overtime|\bnights?\b|\bhours\b|schedule|\bon ?call\b|\blift\b|"
                            r"pounds|\blbs\b|\bdriv|licen[cs]|vehicle|insurance|\bage\b|\b18\b|\b21\b|relative|related to|"
                            r"family|conflict|bond|\bvaccin|covid|\bmedical\b|\bpregnan|religio|union|polic(?:y|ies)|"
                            r"privacy|\bai\b|\bllm|chatgpt|generative|contact|reach|references?|willing|\bable\b|"
                            r"\bopen\b|availab|consent|agree|terms")


def capability_question(f: FormField) -> bool:
    """An ordinary skill/experience choice question the infer level may answer. ALLOWLIST: the whole normalized label
    matches one anchored skill template, its object is a short tool/technology/skill phrase (<= 4 words, no denied
    word), and nothing legal, place, consent, EEO, hard-limit or sensitive appears anywhere."""
    from ..persona.guard import blocked_label
    if not _choice(f) or f.kind == FieldKind.multiselect:
        return False
    n = normalize(f.label)
    ck = canonical_key(f.label)
    if (not n or hard_limit(f) is not None or (ck is not None and ck != "years_experience") or eeo_field(f)
            or never_llm_legal_place(f) or blocked_label(re.sub(r"\b(?:how many )?years of\b", " ", f.label))
            or _INFER_EXCLUDE.search(n) or _ASKS.search(n) or any(_INFER_EXCLUDE.search(normalize(o)) for o in f.options)):
        return False
    for t in _CAP:
        m = t.fullmatch(n)
        if m:
            x = m.group("x").strip()
            return 1 <= len(x.split()) <= 4 and not _X_DENY.search(x)
    return False


_LOW_OPTION = re.compile(r"^(?:no|none|never|not at all)\b|\bno (?:experience|exposure|knowledge|familiarity)\b|"
                         r"\b(?:beginner|novice|basic|minimal|limited|little|not (?:familiar|comfortable|proficient))\b|"
                         r"\bless than\b|^0\b")


def lowest_value(f: FormField) -> Any | None:
    """The honest No / lowest option: a boolean False, or the single negative / lowest-level option."""
    if f.kind == FieldKind.boolean and not f.options:
        return False
    neg = [o for o in f.options if tokens(o)[:1] == ["no"]]
    if len(neg) == 1:
        return neg[0]
    return _unique([o for o in f.options if _LOW_OPTION.search(normalize(o))])


_INFER_GENERIC = frozenset(
    ["are", "you", "would", "be", "comfortable", "able", "willing", "open", "happy", "ok", "okay", "confident", "with", "to", "in", "at", "do", "does", "have", "any", "prior", "previous", "professional", "hands", "on", "practical", "working", "direct", "experience", "familiarity", "exposure", "knowledge", "proficiency", "background", "expertise", "using", "of", "doing", "did", "ever", "previously", "used", "worked", "built", "developed", "managed", "led", "written", "deployed", "designed", "run", "operated", "shipped", "maintained", "done", "made", "how", "familiar", "proficient", "experienced", "skilled", "please", "rate", "select", "indicate", "choose", "what", "is", "your", "level", "current", "comfort", "skill", "consider", "yourself", "fluent", "can", "know", "use", "code", "program", "write", "speak", "read", "making", "make", "a", "an", "the", "and", "or", "for", "this", "role", "position", "job", "our", "we"])


def _stem(w: str) -> str:
    return w[:-1] if len(w) > 3 and w.endswith("s") else w


def topic_supported(label: str, facts: Sequence[str]) -> bool:
    """Every topic word of the question appears in the resume/profile facts (an affirmative claim needs this)."""
    topic = {_stem(w) for w in tokens(label) if w not in _INFER_GENERIC and len(w) > 1}
    have = {_stem(w) for w in tokens(" ".join(facts))}
    return bool(topic) and topic <= have


INFER_PROMPT = (
    "You answer job-application screening questions for ONE candidate using ONLY the resume and profile facts "
    "below. Be conservative:\n"
    "- Answer Yes (or a higher option) ONLY when the facts clearly and directly show it.\n"
    "- When the facts are silent, unclear or only loosely related, answer the honest No or the lowest option.\n"
    "- Never invent experience, tools, numbers or employers. For options, reply with one option exactly.\n"
    "- rationale: at most 20 words naming the supporting fact, or 'not in resume'.\n"
)


def infer_prompt(fields: Sequence[FormField], facts: Sequence[str], role: str) -> str:
    spec = []
    for f in fields:
        row: dict[str, Any] = {"key": f.key, "q": f.label}
        row["options"] = f.options[:30] if f.options else ["Yes", "No"]
        spec.append(row)
    return (f"{INFER_PROMPT}ROLE: {role}\nFACTS:\n" + "\n".join(f"- {x}" for x in facts) +
            f"\nQUESTIONS:\n{json.dumps(spec, separators=(',', ':'))}\n"
            'Return one JSON object keyed by question key: {"<key>": {"value": "...", "confidence": 0..1, '
            '"rationale": "..."}}.')


# ---- pattern tracking ------------------------------------------------------------------------------------------
@dataclass
class Assumption:
    field: FormField
    answer: Answer
    kind: str | None          # pattern key ("assumed.*"), None = never promoted (EEO decline)
    question: str = ""        # what the owner is asked once the kind is promoted


def contact_kind(chans: Sequence[str]) -> str:
    return "assumed.contact." + "+".join(sorted(set(chans)))


def infer_kind(f: FormField) -> str:
    return "assumed.q:" + normalize(f.label)[:120]


def contact_question(chans: Sequence[str], label: str) -> str:
    return (f"When an application asks permission to contact you by {', '.join(sorted(set(chans)))} "
            f"(e.g. \"{label[:80]}\"), should the answer be Yes or No?")


def stored_value(f: FormField, raw: Any) -> Any | None:
    """An owner's stored answer for an assumed kind, coerced to the field; None if it does not fit."""
    s = str(raw or "").strip()
    if not s:
        return None
    if f.kind == FieldKind.boolean and not f.options:
        t = tokens(s)[:1]
        return True if t == ["yes"] else False if t == ["no"] else None
    if f.options:
        got = best_option(s, f.options, cutoff=0.9)
        return None if got is None else [got] if f.kind == FieldKind.multiselect else got
    return s if f.kind in (FieldKind.text, FieldKind.unknown) else None


def write_evidence(data_dir: str | Path, ref: str, rows: list[dict[str, Any]]) -> str | None:
    """assumed.json in the ref's evidence folder (same folder the browser driver uses). Never raises."""
    import hashlib
    try:
        d = Path(data_dir).expanduser() / "evidence" / hashlib.sha256(ref.encode()).hexdigest()[:12]
        d.mkdir(parents=True, exist_ok=True)
        p = d / "assumed.json"
        p.write_text(json.dumps({"ref": ref, "at": datetime.now(UTC).isoformat(timespec="seconds"),
                                 "assumed": rows}, indent=1, default=str), encoding="utf-8")
        return str(p)
    except OSError:
        return None

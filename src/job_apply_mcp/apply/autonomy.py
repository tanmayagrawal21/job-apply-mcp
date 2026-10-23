"""Configurable autonomy level (ADR-0052): what the agent may assume when the owner is not available.

Levels (settings `answers.autonomy`, trusted user config only; see config._RELAXES):
  strict        unresolved fields go to the owner (the behaviour before ADR-0052)
  safe_default  a deterministic SAFE answer where one exists:
                  - contact / marketing permission asks: the declining option (No, or unticked)
                  - "how did you hear about us": the configured default
                  - EEO / demographic: "decline to self-identify" when offered, else blank
                  - optional free text: blank (as before)
                everything else stays unresolved
  infer         safe_default plus ordinary capability / experience choice questions ("comfortable making cold
                calls?", "experience with X?"), answered by the LLM from the resume and profile with a conservative
                prompt: Yes only when the facts clearly support it, otherwise the honest No or the lowest option

Hard limits apply at EVERY level and are checked first: authorization, sponsorship, citizenship, visa; criminal history,
background checks, drug tests; attestations and certifications; legal agreements; salary; start date, relocation and
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
    DEMOGRAPHIC_KEYS,
    best_option,
    canonical_key,
    contact_permission_channels,
    decline_option,
    eeo_class,
    eeo_option_ok,
    is_auth_question,
    never_llm_legal_place,
    normalize,
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
                                       r"\bvisit|\btravel|\boffices?\b|\bmetro\b|time ?zone|\bwhere\b")),
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


# A GENUINE permission ask, by anchored whole-label templates (deny by default). The normalized label must be one head
# below followed only by delivery wording (_ASK_TAIL); anything else ("... WhatsApp training", "... our marketing
# team", "own our SMS product", "experience writing newsletters") is not an ask and is never declined.
_WHO = (r"(?:we|i|our (?:team|recruiters?|recruiting team|talent team|hiring team)|"
        r"(?:\w+ ){0,2}(?:recruiting|recruiters?|talent acquisition|talent team|hiring team))")
_VERB = r"(?:contact|text|call|e ?mail|message|whats ?app|reach out to|reach|send)"
_ASK_HEADS = [re.compile(p) for p in (
    rf"(?:can|may|could|shall) {_WHO} (?:also )?{_VERB}(?: or {_VERB})? you(?P<tail>(?: .*)?)",
    rf"(?:is it ok(?:ay)?|ok(?:ay)?) (?:if we|to) {_VERB}(?: or {_VERB})? you(?P<tail>(?: .*)?)",
    (r"(?:do|would) you (?:like|want|wish) to (?:receive|get|be (?:contacted|notified|kept informed)|hear from us|"
     r"opt in to (?:receive|receiving)?|subscribe to|sign up for)(?P<tail>(?: .*)?)"),
    (r"(?:do you )?(?:opt in|optin|consent|agree) to (?:receive|receiving|be contacted|being contacted|"
     r"be (?:texted|called|emailed|messaged))(?P<tail>(?: .*)?)"),
    (r"i (?:would like|want|wish|agree|consent) to (?:receive|be contacted|subscribe to|sign up for)"
     r"(?P<tail>(?: .*)?)"),
    (r"(?:yes )?(?:please )?(?:sign me up for|subscribe me to|opt me in to|opt in to|opt in|keep me (?:informed|updated|"
     r"posted)|stay in touch|contact me)(?P<tail>(?: .*)?)"),
    (r"(?:(?:do|would) you (?:like|want|wish) to |i (?:would like|want|wish) to )?join (?:our|the|\w+ s) "
     r"(?:talent (?:community|network|pool)|mailing list|newsletter)(?P<tail>(?: .*)?)"),
    (r"(?:sms|text message|text|e ?mail|whats ?app|marketing|newsletter|push|phone|mobile) (?:opt in|optin|updates|"
     r"alerts|notifications|communications|consent|messages|messaging)(?P<tail>(?: .*)?)"),
)]
_ASK_TAIL = frozenset(["updates", "update", "news", "messages", "message", "texts", "text", "emails", "email", "e", "mail", "alerts", "alert", "notifications", "notification", "communications", "communication", "information", "info", "newsletter", "newsletters", "marketing", "offers", "events", "opportunities", "opportunity", "roles", "role", "openings", "opening", "jobs", "job", "positions", "position", "career", "careers", "future", "other", "similar", "new", "relevant", "related", "upcoming", "interview", "interviews", "scheduling", "reminders", "reminder", "application", "applications", "status", "recruiting", "recruitment", "recruiter", "recruiters", "talent", "community", "network", "pool", "mailing", "list", "hiring", "process", "this", "that", "these", "those", "your", "my", "our", "the", "a", "an", "and", "or", "about", "regarding", "on", "for", "by", "via", "through", "over", "at", "with", "from", "to", "of", "in", "us", "you", "me", "sms", "whatsapp", "whats", "app", "phone", "call", "calls", "mobile", "number", "provided", "telegram", "signal", "wechat", "viber", "line", "imessage", "push", "periodic", "occasional", "occasionally", "time", "promotional", "any", "more", "also", "when", "as", "they", "become", "available"])


def contact_channels(f: FormField, company: str = "") -> list[str] | None:
    """Channels of a GENUINE contact / marketing permission ask ("May we text you?", "Can we email or contact you with
    updates?"), else None. The whole label must match an anchored template; the channels come from the ADR-0050
    classifier's interface (contact_permission_channels)."""
    if not _choice(f):
        return None
    from . import mapper
    single = getattr(mapper, "is_single_permission_ask", None)
    if single is not None:              # ADR-0050 (#73): one source of truth for a single-clause permission ask
        chans = single(f.label)
        return list(chans) if chans else None
    n = normalize(f.label)
    for w in [w for w in normalize(company).split() if len(w) > 2]:
        n = re.sub(rf"\b{re.escape(w)}(?: s)?\b", " ", n)
    n = re.sub(r"\s+", " ", n).strip()
    for head in _ASK_HEADS:
        m = head.fullmatch(n)
        if m and all(t in _ASK_TAIL for t in m.group("tail").split()):
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


# ---- infer: ordinary capability / experience questions --------------------------------------------------------
_CAP = [re.compile(p) for p in (
    r"(?:are|would) you (?:be )?(?:comfortable|able|willing|open|happy|ok|okay|confident) (?:with |to |in |at )?[a-z0-9 ]+",
    (r"(?:do|does) you have (?:any )?(?:prior |previous |professional |hands on |practical |working |direct )?"
     r"(?:experience|familiarity|exposure|knowledge|proficiency|background|expertise) (?:with|in|using|of|on|doing) [a-z0-9 ]+"),
    (r"(?:have you|did you) (?:ever |previously )?(?:used|worked (?:with|on|in)|built|developed|managed|led|written|"
     r"deployed|designed|run|operated|shipped|maintained|done|made|sold|programmed|taught|mentored) [a-z0-9 ]+"),
    r"how (?:comfortable|familiar|proficient|experienced|skilled) are you (?:with|in|using|at|doing) [a-z0-9 ]+",
    (r"(?:please )?(?:rate|select|indicate|choose|what is) your (?:level of |current )?(?:experience|proficiency|"
     r"familiarity|comfort|expertise|skill)(?: level)? (?:with|in|using|of|at) [a-z0-9 ]+"),
    r"(?:are you|do you consider yourself) (?:proficient|experienced|familiar|skilled|fluent|comfortable) (?:with|in|at|using) [a-z0-9 ]+",
    r"(?:do|can) you (?:know|use|code in|program in|write|speak|read) [a-z0-9 ]+",
)]
# commitments and conditions are not capabilities: schedules, travel, physical requirements, licences, relatives
_INFER_EXCLUDE = re.compile(r"\bweekends?\b|\bshifts?\b|overtime|\bnights?\b|\bhours\b|schedule|\bon ?call\b|\blift\b|"
                            r"pounds|\blbs\b|\bdriv|licen[cs]|vehicle|insurance|\bage\b|\b18\b|\b21\b|relative|related to|"
                            r"family|conflict|bond|\bvaccin|covid|\bmedical\b|\bpregnan|religio|union|polic(?:y|ies)|"
                            r"privacy|\bai\b|\bllm|chatgpt|generative|contact|reach|references?")


def capability_question(f: FormField) -> bool:
    """An ordinary capability/experience choice question that the infer level may answer. Deny by default: the whole
    normalized label must match one anchored template, and nothing legal, place, consent, EEO or sensitive may appear."""
    from ..persona.guard import blocked_label
    if not _choice(f) or f.kind == FieldKind.multiselect:
        return False
    n = normalize(f.label)
    if (not n or hard_limit(f) is not None or canonical_key(f.label) is not None or eeo_field(f)
            or never_llm_legal_place(f) or blocked_label(f.label)
            or _INFER_EXCLUDE.search(n) or _ASKS.search(n) or any(_INFER_EXCLUDE.search(normalize(o)) for o in f.options)):
        return False
    return any(t.fullmatch(n) for t in _CAP)


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

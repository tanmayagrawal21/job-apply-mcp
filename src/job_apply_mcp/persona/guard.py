"""What stored answers must never touch (ADR-0047 amendment).

Two classes, both fail-closed:
- *blocked labels*: attestations, certifications, consents/agreements, arbitration, criminal history, AI-use,
  clearance/export/"U.S. person", demographic/EEO and anything the mapper treats as sensitive or a stop phrase.
  A stored or persona answer is never recorded, asked, or applied for these.
- *sensitive keys*: authorization, sponsorship, citizenship, clearance, export, privacy acknowledgement, salary floor.
  Only an answer the human gave (origin `user_written`, via elicitation or the TTY CLI) is ever used for them.
  Demographic keys come from `[demographics]` only.
"""
from __future__ import annotations

import re
from collections.abc import Sequence

_EXTRA = re.compile(
    r"\bcertif|\battest|\bconsent|\bagree|\bterms\b|\backnowledg|\barbitrat|\bwaive|\bconvict|\bfelony|\bcriminal|"
    r"\bclearance|\bexport\b|\bitar\b|\bear\b|u\.?s\.? person|\btransgender|\blgbt|\bqueer|\bsexual|\bsms\b|"
    r"text messag|\bmonitor|\bnon[- ]?compete|\bbackground check|\bdrug\b|\bunder oath|\bpenalty|\bperjury|"
    r"\bsponsor|\bvisa\b|work authori[sz]|authori[sz]ed to work|\bcitizen|\bimmigra|right to work|work permit|"
    r"\bsalary\b|\bcompensation\b", re.IGNORECASE)

# Country-agnostic: matches `work_authorized`, `work_authorized:DE`, `authorization.GB.sponsorship`, `country_answer:...`
_SENSITIVE_KEY = re.compile(
    r"^(work_authorized|requires_sponsorship|us_citizen|citizen\w*|clearance|export|itar|privacy_ack|"
    r"acknowledge_privacy_notices|authorization|sponsorship|country_answer|persona\.salary_floor|salary_floor)"
    r"([:._].*)?$")


def demographic_keys() -> set[str]:
    from ..apply.mapper import DEMOGRAPHIC_KEYS
    return set(DEMOGRAPHIC_KEYS)


def is_demographic_key(key: str) -> bool:
    return key in demographic_keys()


def sensitive_key(key: str) -> bool:
    return is_demographic_key(key) or bool(_SENSITIVE_KEY.match(key))


def blocked_label(label: str, stop_phrases: Sequence[str] = ()) -> bool:
    from ..apply.browser_playwright import stop_phrase_hit
    from ..apply.engine import _AI_LABEL, _AI_TOOLS_PROSE, _ATTEST_LABEL, _SENSITIVE_LABEL
    from ..apply.mapper import _DEMOGRAPHIC_LABEL, is_sensitive_question
    if _AI_TOOLS_PROSE.search(label) and not _EXTRA.search(label) and not _ATTEST_LABEL.search(label):
        return False                    # "Which AI tools do you use?" is an ordinary reusable answer
    return bool(_EXTRA.search(label) or _ATTEST_LABEL.search(label) or _AI_LABEL.search(label)
                or _SENSITIVE_LABEL.search(label) or _DEMOGRAPHIC_LABEL.search(label)
                or is_sensitive_question(label) or (stop_phrases and stop_phrase_hit(label, list(stop_phrases))))

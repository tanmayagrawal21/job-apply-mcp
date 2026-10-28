"""Why a person has to act: every handoff reason is a hard_limit, an unknown_fact or a tool_gap (ADR-0067).

- hard_limit: the tool must never do it itself (visible CAPTCHA or comprehension check, attestation / legal terms,
  secrets, the owner's own final-click or approval choice when no consent covers it).
- unknown_fact: the answer is genuinely not known (no profile fact, needs the user's own words).
- tool_gap: everything else. The tool should have finished but could not (a field it failed to fill, a page that
  did not render, an unsupported site, a missing component, an unclear outcome). These are counted in run reports so
  they can be driven down.

Inside an active consent window (ADR-0048) a step that the consent covers (approval, a passive widget, a login, an
emailed code) is a tool_gap, never a reason to hand work to the person. Pure functions; no I/O except an optional
consent lookup.
"""
from __future__ import annotations

import re
from typing import Any

HARD_LIMIT, UNKNOWN_FACT, TOOL_GAP = "hard_limit", "unknown_fact", "tool_gap"
CATEGORIES = (HARD_LIMIT, UNKNOWN_FACT, TOOL_GAP)
# statuses where a person is asked to act (or the outcome is unclear)
HANDOFF_STATUSES = frozenset({"needs_human", "incomplete", "handoff", "roadblock", "unknown", "needs_input",
                              "needs_review", "awaiting_code", "awaiting_approval", "quota_blocked",
                              "captcha_rejected"})   # ADR-0066: terminal captcha verdict -> hard_limit

# Checked FIRST: a detail matching a hard limit is a hard_limit even when a consented pattern also matches.
_HARD = re.compile(
    r"captcha|hcaptcha|turnstile|cloudflare|bot[- ]check|human verification|visible (?:\w+ )?challenge|"
    r"comprehension|anti-automation|attestation|acknowledg|personally (?:completed|wrote|written)|"
    r"\bai[- ](?:use|usage|tools?|generated|assist)|legal|terms|consent field|stop phrase|"
    r"secret|password|passcode|api[ _-]?key|\btoken\b|credential|"
    r"final_click=human|click submit in the open tab|quota|daily limit|control: block",
    re.IGNORECASE)
# Fallback only: structure (unresolved / client_tasks / conflict hints / status) is checked before any wording.
_UNKNOWN = re.compile(
    r"\bunresolved\b|no explicit profile answer|profile (?:gap|question|answer)|needs_input|"
    r"not approved yet|prose (?:left )?open|essay|client_tasks|ask the user|"
    r"(?:stayed|still|left) empty|required fields?|conflict|disagree|candidate journal", re.IGNORECASE)
# statuses whose reason is, by construction, an answer the tool does not have (ADR-0067 control review)
_UNKNOWN_STATUSES = frozenset({"needs_input", "needs_review", "incomplete"})
# steps an active consent scope automates: (pattern, scope)
_CONSENTED = (
    (re.compile(r"awaiting_approval|approval required|awaiting approval", re.IGNORECASE), "approve_submissions"),
    (re.compile(r"\blogin\b|sign[- ]in|account", re.IGNORECASE), "credentials"),
    (re.compile(r"verification code|\bcode\b|\botp\b|confirm (?:your )?email", re.IGNORECASE), "otp_unattended"),
)   # no captcha entry: any captcha / bot check is a hard_limit (checked first)
# without consent these are the owner's to do (secrets, captcha, own approval)
_HARD_NO_CONSENT = re.compile(r"awaiting_approval|awaiting approval|\blogin\b|sign[- ]in|verification|\bcode\b|"
                              r"\botp\b|captcha", re.IGNORECASE)


def _consented(settings: Any, scope: str) -> bool:
    if settings is None:
        return False
    try:
        from .. import consent
        return consent.active(settings, scope) is not None
    except Exception:  # noqa: BLE001 - classification must never fail
        return False


def _structural(unresolved: Any, client_tasks: Any) -> str | None:
    """Category from the result's own lists, preferred over any wording: an attestation task is a hard_limit; a
    journal/profile conflict, an unresolved field or any other client task is an unknown_fact."""
    tasks = [t for t in (client_tasks or []) if isinstance(t, dict)]
    if any(t.get("needs_human") for t in tasks):
        return HARD_LIMIT
    if tasks or unresolved:
        return UNKNOWN_FACT
    return None


def categorize(status: str | None, detail: Any, settings: Any = None, *, unresolved: Any = None,
               client_tasks: Any = None) -> str | None:
    """Category of a handoff (None for a status that is not a handoff, e.g. submitted or skipped). Order: hard-limit
    wording, then the result's structure (unresolved / client_tasks / conflicts / status), then consented steps,
    then wording fallbacks."""
    st = str(status or "")
    if st not in HANDOFF_STATUSES:
        return None
    text = f"{st} {detail or ''}"
    if _HARD.search(text):                   # hard limits first, before any consented pattern
        return HARD_LIMIT
    if (c := _structural(unresolved, client_tasks)) is not None:
        return c
    first = detail[0] if isinstance(detail, (list, tuple)) and detail else detail
    if str(first or "").startswith("tool_gap:"):   # ADR-0074: a reason the tool itself tagged as its own gap
        return TOOL_GAP
    if st in _UNKNOWN_STATUSES:
        return UNKNOWN_FACT
    if _UNKNOWN.search(text):                # wording fallback for rows without structure (run reports)
        return UNKNOWN_FACT
    for pat, scope in _CONSENTED:            # a consented step left to a person is the tool's gap
        if pat.search(text) and _consented(settings, scope):
            return TOOL_GAP
    if st in ("awaiting_code", "awaiting_approval") or _HARD_NO_CONSENT.search(text):
        return HARD_LIMIT
    return TOOL_GAP


def in_window(settings: Any) -> bool:
    return _consented(settings, "approve_submissions")


def category_hint(cat: str | None, window: bool) -> str:
    """One line for the client. In a consent window a tool_gap never becomes the person's job."""
    if cat == TOOL_GAP:
        return ("tool_gap inside a consent window: retry through the tool or file request_feature; do not hand "
                "this to the user" if window else "tool_gap: the tool could not finish; tell the user and file "
                "request_feature")
    if cat == UNKNOWN_FACT:
        return "unknown_fact: ask the user for this fact once; the tool remembers it"
    if cat == HARD_LIMIT:
        return "hard_limit: only the user may do this"
    return ""


def count(rows: list[dict[str, Any]], key: str = "cat") -> dict[str, int]:
    out = {c: 0 for c in CATEGORIES}
    for r in rows:
        c = r.get(key)
        if c in out:
            out[c] += 1
    return {k: v for k, v in out.items() if v}

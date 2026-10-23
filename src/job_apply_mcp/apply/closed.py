"""Closed postings and anti-automation challenges (ADR-0056). Pure helpers, unit-tested offline.

A closed posting is reported as `skipped` (closed), never as an owner action. A "comprehension check" (or any
other question whose purpose is to catch automated applicants) is a visible challenge: it is never answered, never
offered to the client as a task, and the application becomes a human handoff with a clear reason.
"""
from __future__ import annotations

import re
from typing import Any

# Markers that the employer has closed the posting. Field labels on a closed Ashby form read e.g.
# "Name (NOT USED; APPLICATIONS CLOSED)".
_CLOSED_TEXT = re.compile(
    r"\bapplications?\s+(?:are\s+|is\s+)?(?:now\s+)?closed\b"
    r"|\bno\s+longer\s+(?:accepting|taking|receiving)\s+(?:applications|applicants|candidates)\b"
    r"|\b(?:this|the)\s+(?:job|position|posting|role)\s+(?:is\s+|has\s+been\s+)?(?:closed|no\s+longer\s+available)\b",
    re.IGNORECASE)
# "NOT USED" only counts inside a FIELD LABEL, in capitals or parentheses (a deprecated form, not prose).
_NOT_USED_LABEL = re.compile(r"\(\s*not\s+used\b|\bNOT USED\b")
# A title that announces the closure ("[CLOSED] Engineer", "Engineer (closed)"). A bare word such as
# "Closed-loop controls" never matches.
_CLOSED_TITLE = re.compile(r"^\s*(?:closed|filled)\s*(?::|\s[-\u2013|]\s)|[(\[]\s*(?:closed|filled|no longer accepting[^)\]]*)\s*[)\]]",
                           re.IGNORECASE)

# Anti-automation questions: answering one is solving a bot check, which this tool never does (ADR-0036).
_CHALLENGE = re.compile(
    r"comprehension\s+check\b|attention\s+check\b|\banti[- ]?(?:bot|automation)\s+(?:check|question|test)\b"
    r"|\bare\s+you\s+(?:a\s+)?(?:human|robot|bot)\b|\bprove\s+(?:that\s+)?you(?:'re|\s+are)\s+(?:a\s+)?human\b"
    r"|\bverify\s+(?:that\s+)?you(?:'re|\s+are)\s+(?:a\s+)?human\b|\bhuman\s+verification\b"
    r"|\bAI[- ]generated\s+applications?\s+check\b",
    re.IGNORECASE)


def closed_text(text: str) -> str | None:
    """The closed-posting marker found in free text (page text, description), else None."""
    m = _CLOSED_TEXT.search(text or "")
    return m.group(0).strip() if m else None


def closed_label(label: str) -> str | None:
    """A closed marker in one FIELD LABEL (stricter markers allowed: 'NOT USED')."""
    m = _CLOSED_TEXT.search(label or "") or _NOT_USED_LABEL.search(label or "")
    return m.group(0).strip(" (") if m else None


def closed_reason(*, title: str = "", description: str = "", labels: list[str] | tuple[str, ...] = ()) -> str | None:
    """Why the posting is closed ('closed: <marker>'), or None. Labels are checked first (the strongest signal),
    then the title, then the description."""
    for lab in labels:
        if (m := closed_label(lab)):
            return f"closed: field label says '{m}'"
    if title and _CLOSED_TITLE.search(title):
        return "closed: title marks the posting closed"
    if (m := closed_text(description)):
        return f"closed: posting says '{m}'"
    return None


def challenge_label(label: str) -> bool:
    """True when the question is an anti-automation challenge (never answered by this tool)."""
    return bool(_CHALLENGE.search(label or ""))


def challenge_fields(fields: list[Any]) -> list[Any]:
    """The form fields whose label is an anti-automation challenge."""
    return [f for f in fields if challenge_label(getattr(f, "label", "") or "")]


CHALLENGE_REASON = ("visible challenge: anti-automation question ({labels}); never answered or offered as a task; "
                    "a human must complete this application")

"""Normalized question keys, role families and the label patterns the persona policies recognise."""
from __future__ import annotations

import re


def normalize_question(s: str) -> str:
    """Same normalization as profile.normalize_question (duplicated to keep persona free of profile imports)."""
    return " ".join(re.sub(r"[^a-z0-9 ]+", " ", s.lower()).split())


_AI_TOOLS = re.compile(r"\bai\b.*\b(tools?|assistants?)\b|\b(tools?|assistants?)\b.*\bai\b|\bllms?\b.*\btools?\b",
                       re.IGNORECASE)
_EXCEPTIONAL = re.compile(
    r"exceptional (work|ability|achievement)|most (impressive|significant|proud)|"
    r"(project|accomplishment|achievement) (you are|you're) (most )?proud|something you(?:'ve| have)? (built|made|created)|"
    r"example of (your )?(best|exceptional|outstanding) work|evidence of exceptional", re.IGNORECASE)

_FAMILIES: list[tuple[str, tuple[str, ...]]] = [
    ("ml", ("machine learning", "ml ", "ai ", "data scientist", "research scientist", "nlp", "deep learning")),
    ("data", ("data", "analytics", "analyst", "bi ")),
    ("security", ("security", "infosec")),
    ("devops", ("devops", "sre", "reliability", "platform engineer", "infrastructure", "cloud")),
    ("mobile", ("ios", "android", "mobile")),
    ("frontend", ("front end", "frontend", "front-end", "ui engineer", "web developer")),
    ("software", ("software", "engineer", "developer", "backend", "back end", "full stack", "fullstack")),
    ("design", ("design", "ux", "ui ")),
    ("product", ("product manager", "product owner", "program manager")),
    ("sales", ("sales", "account executive", "business development")),
]


def role_family(title: str) -> str:
    t = f" {title.lower()} "
    for fam, needles in _FAMILIES:
        if any(n in t for n in needles):
            return fam
    return "other"


def is_ai_tools_label(label: str) -> bool:
    return bool(_AI_TOOLS.search(label))


def is_exceptional_label(label: str) -> bool:
    return bool(_EXCEPTIONAL.search(label))


def gap_key(label: str, canonical: str | None) -> str:
    """Stable key for a question: a mapper canonical key when there is one, `ai_tools` for AI-tool questions,
    else `q:<normalized label>` (bounded)."""
    if canonical:
        return canonical
    if is_ai_tools_label(label):
        return "ai_tools"
    if is_exceptional_label(label):
        return "persona.exceptional_work"
    return "q:" + normalize_question(label)[:120]

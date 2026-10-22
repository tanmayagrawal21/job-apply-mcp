"""Deterministic request classification (no LLM needed; callers may override with `kind`)."""
from __future__ import annotations

import re

KINDS = ("platform_adapter", "control", "config", "new_tool", "bug")

_RULES: list[tuple[str, tuple[str, ...]]] = [
    ("bug", ("bug", "crash", "fails", "failing", "broken", "error", "exception", "regression", "timeout", "wrong")),
    ("platform_adapter", ("adapter", "platform", "ats", "workday", "greenhouse", "lever", "ashby", "icims",
                          "smartrecruiters", "taleo", "jobvite", "bamboohr", "support for")),
    ("control", ("control", "rule", "gate", "policy", "guard", "stop phrase", "attestation", "limit", "block")),
    ("config", ("config", "setting", "default", "threshold", "num_ctx", "tier", "knob", "option")),
    ("new_tool", ("new tool", "mcp tool", "endpoint", "tool that", "add a tool", "expose")),
]
# Strong signals beat bug-vocabulary: "add an adapter for X" mentions no failure.
_PRIORITY = ("platform_adapter", "new_tool", "control", "config", "bug")


def classify(description: str, kind: str | None = None) -> str:
    if kind:
        if kind not in KINDS:
            raise ValueError(f"unknown kind '{kind}'; known: {list(KINDS)}")
        return kind
    text = description.lower()
    scores = {k: sum(1 for w in words if w in text) for k, words in _RULES}
    best = max(_PRIORITY, key=lambda k: (scores[k], -_PRIORITY.index(k)))
    return best if scores[best] else "new_tool"


def slugify(text: str, n: int = 40) -> str:
    s = re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")
    return (s[:n].strip("-")) or "change"


SENSITIVE_WORDS = ("captcha", "stop_phrase", "stop phrase", "attestation", "credential", "password", "secret",
                   "dry_run", "dry run", "submit without", "bypass", "verification code")


def risk_for(kind: str, description: str) -> str:
    d = description.lower()
    if any(w in d for w in SENSITIVE_WORDS) or kind == "control":
        return "high"
    return {"config": "low", "bug": "medium", "platform_adapter": "medium", "new_tool": "medium"}[kind]

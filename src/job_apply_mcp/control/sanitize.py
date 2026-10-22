"""Untrusted-content sanitizer: text scraped from pages that addresses an AI is data, never instructions.

`sanitize_untrusted(text)` removes sentences that try to instruct a model and strips invisible characters
(a common hiding place). Call it on any scraped text before it reaches an LLM.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field

_INVISIBLE = re.compile(r"[\u200b\u200c\u200d\u2060\ufeff\u202a-\u202e\u2066-\u2069]")

_PATTERNS = [re.compile(p, re.IGNORECASE) for p in (
    r"\bignore (?:all |any )?(?:the )?(?:previous|prior|above|earlier) (?:instructions?|prompts?|messages?)",
    r"\bdisregard (?:all |any )?(?:the )?(?:previous|prior|above|earlier|your) ",
    r"\b(?:if|when) you are (?:an? )?(?:ai|llm|language model|chatbot|assistant|bot|agent)\b",
    r"\b(?:note|message|instructions?) (?:to|for) (?:the )?(?:ai|llm|assistant|model|agent|bot)s?\b",
    r"\bas an? (?:ai|llm|language model)\b.*\b(?:must|should|always|never)\b",
    r"\b(?:ai|llm|chatgpt|claude|gpt)[- ]?(?:agents?|assistants?|models?|applicants?)\b.*\b(?:must|should|please|do not|don't)\b",
    r"\byou (?:must|should|are required to) (?:now )?(?:include|mention|add|insert|write|say|output|reveal|print)\b.*\b(?:word|phrase|code|prompt)\b",
    r"\b(?:reveal|print|repeat|show) (?:your |the )?(?:system|hidden|initial) prompt\b",
    r"<\|?(?:im_start|im_end|system|assistant)\|?>",
    r"^\s*(?:system|assistant|developer)\s*:",
    r"\bnew instructions?\s*:",
    r"\bdo not (?:tell|inform|mention (?:this )?to) the (?:user|human|candidate)\b",
)]


@dataclass
class SanitizeResult:
    text: str
    removed: list[str] = field(default_factory=list)   # trimmed snippets that were stripped

    @property
    def flagged(self) -> bool:
        return bool(self.removed)


def sanitize_untrusted(text: str, *, extra_patterns: list[str] | None = None) -> SanitizeResult:
    extra = [re.compile(p, re.IGNORECASE) for p in (extra_patterns or [])]
    cleaned = _INVISIBLE.sub("", text)
    hidden = len(cleaned) != len(text)
    removed: list[str] = []
    kept_lines: list[str] = []
    for line in cleaned.splitlines():
        parts = re.split(r"(?<=[.!?])\s+", line)
        kept: list[str] = []
        for part in parts:
            if any(p.search(part) for p in (*_PATTERNS, *extra)):
                removed.append(part.strip()[:80])
            else:
                kept.append(part)
        kept_lines.append(" ".join(kept))
    if hidden:
        removed.append("invisible-characters")
    return SanitizeResult(text="\n".join(kept_lines), removed=removed)

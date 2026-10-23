"""Run feedback (ADR-0045): structured feedback becomes overlays on policy / profile answers; free text is a note.

Feedback is stored in the local DB. At the start of every run all structured feedback is replayed in order onto the
in-memory settings and profile (idempotent), and feedback not yet seen by a run is listed as `applied`. Nothing here
writes the user's config or profile files.
"""
from __future__ import annotations

import math
import re
from typing import Any

KINDS = ("deny_company", "deny_title", "answer", "salary", "run_size", "note")
SALARY_KEYS = ("salary_floor", "default_ask", "base_salary")
# Answers that are legal or biographical statements are never changed through a tool call: edit profile.toml.
_SENSITIVE = re.compile(
    r"authori[sz]|sponsor|visa|citizen|clearance|criminal|convict|felony|gender|sex\b|sexual|race|ethnic|hispanic|"
    r"latin|veteran|disab|pronoun|over\s*18|\bage\b|attest|certif|signature|salary|compensation|\bpay\b",
    re.IGNORECASE)


class FeedbackError(ValueError):
    pass


def _pattern(v: Any) -> str:
    p = str(v or "").strip()
    if not p:
        raise FeedbackError("pattern is required")
    if len(p) > 200:
        raise FeedbackError("pattern is longer than 200 characters")
    try:
        re.compile(p)
    except re.error as e:
        raise FeedbackError(f"pattern is not a valid regex: {e}") from None
    return p


def _nonneg_int(v: Any, key: str) -> int | None:
    if v is None:
        return None
    if isinstance(v, bool) or not isinstance(v, (int, float, str)):
        raise FeedbackError(f"{key} must be a whole number")
    try:
        n = float(v)
    except ValueError:
        raise FeedbackError(f"{key} must be a whole number") from None
    if not math.isfinite(n) or n < 0 or n != int(n):
        raise FeedbackError(f"{key} must be a whole number >= 0")
    return int(n)


def normalize(feedback: str | dict[str, Any], *, item: dict[str, Any] | None, run_size: int,
              cap: int) -> tuple[str, dict[str, Any]]:
    """Validate one feedback value into (kind, payload). Free text -> ("note", {"text"})."""
    if isinstance(feedback, str):
        text = feedback.strip()
        if not text:
            raise FeedbackError("empty feedback")
        return "note", {"text": text[:2000]}
    if not isinstance(feedback, dict):
        raise FeedbackError("feedback must be text or an object with a 'kind'")
    kind = str(feedback.get("kind") or feedback.get("type") or "").strip().lower()
    if kind not in KINDS:
        raise FeedbackError(f"unknown kind {kind!r}; one of {list(KINDS)}")
    if kind == "note":
        return normalize(str(feedback.get("text") or ""), item=item, run_size=run_size, cap=cap)
    if kind in ("deny_company", "deny_title"):
        col = "company" if kind == "deny_company" else "title"
        pat = feedback.get("pattern")
        if not pat and item and item.get(col):
            pat = "^" + re.escape(str(item[col])) + "$"   # deny exactly the item's company/title
        return kind, {"pattern": _pattern(pat)}
    if kind == "answer":
        pat = _pattern(feedback.get("pattern") or feedback.get("question"))
        if _SENSITIVE.search(pat):
            raise FeedbackError("answers about authorization, demographics, attestations or pay are changed in "
                                "profile.toml, not through feedback")
        val = str(feedback.get("value") if feedback.get("value") is not None else "").strip()
        if not val or len(val) > 500:
            raise FeedbackError("value is required (at most 500 characters)")
        return kind, {"pattern": pat, "value": val}
    if kind == "salary":
        out = {k: n for k in SALARY_KEYS if (n := _nonneg_int(feedback.get(k), k)) is not None}
        if not out:
            raise FeedbackError(f"salary feedback needs one of {list(SALARY_KEYS)}")
        return kind, out
    # run_size: an absolute value or a delta, resolved now and clamped to [1, cap]
    if feedback.get("value") is not None:
        n = _nonneg_int(feedback.get("value"), "value")
    elif feedback.get("delta") is not None:
        d = feedback.get("delta")
        if isinstance(d, bool) or not isinstance(d, (int, str)) or not re.fullmatch(r"[+-]?\d+", str(d).strip()):
            raise FeedbackError("delta must be a whole number such as 5 or -3")
        n = run_size + int(str(d).strip())
    else:
        raise FeedbackError("run_size feedback needs 'value' or 'delta'")
    return kind, {"value": max(1, min(cap, int(n or 0)))}


def describe(kind: str, payload: dict[str, Any]) -> str:
    if kind == "deny_company":
        return f"deny company {payload['pattern']}"
    if kind == "deny_title":
        return f"deny title {payload['pattern']}"
    if kind == "answer":
        return f"answer for '{payload['pattern']}' set"
    if kind == "salary":
        return "salary " + ", ".join(f"{k}={v}" for k, v in payload.items())
    if kind == "run_size":
        return f"max_applications={payload['value']}"
    return "note: " + payload.get("text", "")[:120]


def _answers(profile: Any) -> dict[str, str] | None:
    p = getattr(profile, "p", profile)
    a = getattr(p, "answers", None)
    return a if isinstance(a, dict) else None


def apply_overlays(settings: Any, profile: Any, rows: list[dict[str, Any]]) -> tuple[int | None, list[str]]:
    """Replay structured feedback onto settings.policy and the profile's answers. Returns (run_size, problems)."""
    pol, run_size, problems = settings.policy, None, []
    for r in rows:
        kind, p = r["kind"], r["payload"]
        if kind == "deny_company" and p["pattern"] not in pol.exclude_employer_patterns:
            pol.exclude_employer_patterns.append(p["pattern"])
        elif kind == "deny_title" and p["pattern"] not in pol.exclude_title_patterns:
            pol.exclude_title_patterns.append(p["pattern"])
        elif kind == "salary":
            for k, v in p.items():
                if k in SALARY_KEYS:
                    setattr(pol, k, int(v))
        elif kind == "run_size":
            run_size = int(p["value"])
        elif kind == "answer":
            ans = _answers(profile)
            if ans is None:
                problems.append(f"feedback #{r['id']}: no profile loaded; answer not applied")
            else:
                ans[p["pattern"]] = p["value"]
    return run_size, problems

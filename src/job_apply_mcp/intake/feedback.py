"""Run feedback (ADR-0045): structured feedback becomes overlays on policy / profile answers; free text is a note.

Feedback is stored in the local DB. At the start of every run all structured feedback is replayed in order onto the
in-memory settings and profile (idempotent), and feedback not yet seen by a run is listed as `applied`. Nothing here
writes the user's config or profile files.
"""
from __future__ import annotations

import math
import re
from typing import Any

KINDS = ("deny_company", "deny_title", "answer", "profile_answer", "salary", "run_size", "note")
SALARY_KEYS = ("salary_floor", "default_ask", "base_salary", "expected")
SCOPES = ("global", "company", "role_family")
# Legal / biographical statements (authorization, demographics, attestations) are not profile-answer overlays: they
# go to the profile builder's stored answers (answer_profile_question, ADR-0047), pending until that path exists.
_PROFILE_Q = re.compile(
    r"authori[sz]|sponsor|visa|citizen|clearance|criminal|convict|felony|gender|sex\b|sexual|race|ethnic|hispanic|"
    r"latin|veteran|disab|pronoun|over\s*18|\bage\b|attest|certif|signature", re.IGNORECASE)
_PAY = re.compile(r"salary|compensation|\bpay\b", re.IGNORECASE)
_BASE: dict[int, tuple[Any, dict[str, Any]]] = {}   # id(obj) -> (obj, configured salary values), kept alive


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
        if _PROFILE_Q.search(pat):   # legal / biographical: route to the profile builder's stored answers
            return normalize({**feedback, "kind": "profile_answer", "question": pat}, item=item, run_size=run_size,
                             cap=cap)
        if _PAY.search(pat):
            raise FeedbackError("pay is changed with {\"kind\":\"salary\", ...} (floor, default_ask, expected, delta)")
        return kind, {"pattern": pat, "value": _value(feedback)}
    if kind == "profile_answer":
        return kind, _profile_answer(feedback, item)
    if kind == "salary":
        out: dict[str, Any] = {k: n for k in SALARY_KEYS if (n := _nonneg_int(feedback.get(k), k)) is not None}
        for k in ("delta", "percent"):
            v = feedback.get(k)
            if v is None:
                continue
            if isinstance(v, bool) or not re.fullmatch(r"[+-]?\d+", str(v).strip()):
                raise FeedbackError(f"{k} must be a whole number such as 5000 or -5")
            out[k] = int(str(v).strip())
        if not out:
            raise FeedbackError(f"salary feedback needs one of {[*SALARY_KEYS, 'delta', 'percent']}")
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


def _value(feedback: dict[str, Any]) -> str:
    val = str(feedback.get("value") if feedback.get("value") is not None else "").strip()
    if not val or len(val) > 500:
        raise FeedbackError("value is required (at most 500 characters)")
    return val


def _profile_answer(feedback: dict[str, Any], item: dict[str, Any] | None) -> dict[str, Any]:
    """Payload for answer_profile_question(key, value, scope, scope_value) (ADR-0047): the mapper's canonical key
    when the question has one, else `q:<normalized question>` (the profile builder's key scheme)."""
    key = str(feedback.get("key") or "").strip()
    if not key:
        from ..apply.mapper import canonical_key
        from ..apply.mapper import normalize as norm_label
        q = _pattern(feedback.get("question") or feedback.get("pattern"))
        key = canonical_key(q) or "q:" + " ".join(re.sub(r"[^a-z0-9 ]+", " ", norm_label(q).lower()).split())[:120]
    scope, _, sv = str(feedback.get("scope") or "global").partition(":")
    scope = scope.strip().lower().replace("-", "_")
    if scope not in SCOPES:
        raise FeedbackError(f"scope must be one of {list(SCOPES)}")
    sv = str(feedback.get("scope_value") or sv).strip()
    if scope == "company" and not sv and item:
        sv = str(item.get("company") or "")
    if scope != "global" and not sv:
        raise FeedbackError(f"scope {scope!r} needs scope_value")
    return {"key": key[:160], "value": _value(feedback), "scope": scope, "scope_value": sv}


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
    if kind == "profile_answer":
        sc = payload["scope"] + (f":{payload['scope_value']}" if payload.get("scope_value") else "")
        return f"profile answer {payload['key']} ({sc})"
    return "note: " + payload.get("text", "")[:120]


def _answers(profile: Any) -> dict[str, str] | None:
    p = getattr(profile, "p", profile)
    a = getattr(p, "answers", None)
    return a if isinstance(a, dict) else None


def _salary_rules(profile: Any) -> Any:
    return getattr(getattr(profile, "p", profile), "salary", None)


def _baseline(obj: Any, attrs: tuple[str, ...]) -> dict[str, Any]:
    """Configured values, captured the first time feedback touches `obj`, so replays start from the config."""
    if obj is None:
        return {}
    hit = _BASE.get(id(obj))
    if hit is None or hit[0] is not obj:
        hit = _BASE[id(obj)] = (obj, {a: getattr(obj, a, None) for a in attrs})
    return hit[1]


def _apply_salary(pol: Any, rules: Any, rows: list[dict[str, Any]], problems: list[str]) -> None:
    """Reset to the configured values, then apply salary feedback in order. The configured floor is a hard lower
    bound: feedback may raise salary_floor / base_salary but never lower them, and every ask (default_ask, the
    profile's expected salary) is clamped to at least the effective floor."""
    base = _baseline(pol, ("salary_floor", "default_ask", "base_salary"))
    rbase = _baseline(rules, ("expected",))
    for k, v in base.items():
        setattr(pol, k, v)
    if rules is not None:
        rules.expected = rbase.get("expected")
    cfg_floor, cfg_base = base.get("salary_floor"), base.get("base_salary")
    for r in rows:
        p = r["payload"]
        if "salary_floor" in p:
            pol.salary_floor = max(p["salary_floor"], cfg_floor or 0)
        if "base_salary" in p:
            pol.base_salary = max(p["base_salary"], cfg_base or 0, cfg_floor or 0)
        if "default_ask" in p:
            pol.default_ask = p["default_ask"]
        if "expected" in p:
            if rules is None:
                problems.append(f"feedback #{r['id']}: no profile loaded; expected salary not applied")
            else:
                rules.expected = p["expected"]
        if "delta" in p or "percent" in p:
            for obj, attr in ((pol, "default_ask"), (rules, "expected")):
                cur = getattr(obj, attr, None) if obj is not None else None
                cur = cur if cur is not None else pol.salary_floor
                if obj is None or cur is None:
                    continue
                cur = cur + p.get("delta", 0)
                cur = round(cur * (1 + p.get("percent", 0) / 100))
                setattr(obj, attr, int(cur))
        _clamp(pol, rules, problems)   # after every step: an ask is never below the floor


def _clamp(pol: Any, rules: Any, problems: list[str]) -> None:
    floor = pol.salary_floor
    if floor is None:
        return
    for obj, attr in ((pol, "default_ask"), (rules, "expected")):
        v = getattr(obj, attr, None) if obj is not None else None
        if v is not None and v < floor:
            setattr(obj, attr, floor)
            note = f"salary: {attr} raised to the floor {floor}"
            if note not in problems:
                problems.append(note)


def apply_overlays(settings: Any, profile: Any, rows: list[dict[str, Any]]) -> tuple[int | None, list[str]]:
    """Replay structured feedback onto settings.policy, the profile's answers and its salary rule. profile_answer
    rows are not overlays (the runner delivers them to the profile builder). Returns (run_size, problems)."""
    pol, run_size, problems = settings.policy, None, []
    salary = [r for r in rows if r["kind"] == "salary"]
    if salary or id(pol) in _BASE:
        _apply_salary(pol, _salary_rules(profile), salary, problems)
    for r in rows:
        kind, p = r["kind"], r["payload"]
        if kind == "deny_company" and p["pattern"] not in pol.exclude_employer_patterns:
            pol.exclude_employer_patterns.append(p["pattern"])
        elif kind == "deny_title" and p["pattern"] not in pol.exclude_title_patterns:
            pol.exclude_title_patterns.append(p["pattern"])
        elif kind == "run_size":
            run_size = int(p["value"])
        elif kind == "answer":
            ans = _answers(profile)
            if ans is None:
                problems.append(f"feedback #{r['id']}: no profile loaded; answer not applied")
            else:
                ans[p["pattern"]] = p["value"]
    return run_size, problems

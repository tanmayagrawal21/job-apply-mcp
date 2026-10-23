"""Run feedback (ADR-0045): structured feedback becomes overlays on policy / profile answers; free text is a note.

Feedback is stored in the local DB with its origin: `user` (the CLI on a TTY, or a human confirmation) or `client`
(the MCP tool). At the start of every run the structured feedback that may apply is replayed in order onto the
in-memory settings and profile (idempotent); feedback not yet seen by a run is listed as `applied`. Client-origin
answers never become owner-authority answers without a human confirmation (see `needs_confirmation`). Nothing here
writes the user's config or profile files.
"""
from __future__ import annotations

import math
import re
from typing import Any

KINDS = ("deny_company", "deny_title", "answer", "profile_answer", "salary", "run_size", "note")
SALARY_KEYS = ("salary_floor", "default_ask", "base_salary", "expected")
SCOPES = ("global", "company", "role_family")
# Legal, biographical, consent and attestation topics are never profile-answer overlays: they go to the profile
# builder's stored answers (answer_profile_question, ADR-0047). Deny-by-default: this list is ADDED to the mapper's
# and engine's own sensitivity checks (see is_restricted_question); it is phrased for any country, not one.
_PROFILE_Q = re.compile(
    r"authori[sz]|sponsor|visa|citizen|nationalit|national (id|insurance|identity)|passport|immigra|residen|"
    r"green card|work permit|right to work|eligib|legally|permitted|\blicen[cs]e|clearance|security vetting|export|"
    r"itar|\bear\b|\bu\W*s\W*persons?\b|sanction|criminal|convict|felony|offen[cs]e|background|drug|alcohol|"
    r"medical|health|pregnan|gender|\bsex\b|sexual|race|racial|ethnic|caste|religio|hispanic|latin|veteran|military|"
    r"armed forces|disab|pronoun|marital|family status|union|political|over\s*1[68]|\bage\b|date of birth|birth|"
    r"attest|certif|declar|acknowledg|consent|agree|accept|arbitrat|terms|condition|policy|privacy|signature|"
    r"\bsms\b|text message|whatsapp|marketing|true and|accurate|complete and|truthful|\bai\b|artificial|"
    r"\bllms?\b|chatgpt|generative|conflict of interest|non[- ]?compete|tax|bank|ssn|social security", re.IGNORECASE)
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


def is_restricted_question(pattern: str, stop_phrases: list[str] | tuple[str, ...] = ()) -> bool:
    """Deny-by-default gate for `answer` overlays: True when the pattern is a canonical (structured) question, or any
    of the mapper's / engine's sensitivity, demographic, attestation, consent or AI-use checks, the policy stop
    phrases, or the intake list hit it. Only plain, non-sensitive questions ("favorite editor") pass."""
    from ..apply.engine import _HUMAN_LABEL, _SENSITIVE_LABEL
    from ..apply.mapper import _DEMOGRAPHIC_LABEL, canonical_key, is_sensitive_question
    text = pattern.removeprefix("re:")
    plain = re.sub(r"[\\^$.|?*+()\[\]{}]", " ", text)   # a regex pattern is judged on its words
    for t in (text, plain, f"Do you {plain.strip()}?"):   # also judged as a sample question label
        if canonical_key(t) is not None or is_sensitive_question(t):
            return True
        if any(r.search(t) for r in (_PROFILE_Q, _SENSITIVE_LABEL, _HUMAN_LABEL, _DEMOGRAPHIC_LABEL)):
            return True
        if any(p.lower() in t.lower() for p in stop_phrases if p.strip()):
            return True
    return False


def normalize(feedback: str | dict[str, Any], *, item: dict[str, Any] | None, run_size: int,
              cap: int, stop_phrases: list[str] | tuple[str, ...] = ()) -> tuple[str, dict[str, Any]]:
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
    if kind == "answer":
        pat = _pattern(feedback.get("pattern") or feedback.get("question"))
        if _PAY.search(pat):
            raise FeedbackError("pay is changed with {\"kind\":\"salary\", ...} (floor, default_ask, expected, delta)")
        if is_restricted_question(pat, stop_phrases):   # route to the profile builder's stored answers
            return "profile_answer", _profile_answer({**feedback, "question": pat}, item)
        return kind, {"pattern": pat, "value": _value(feedback)}
    if kind in ("deny_company", "deny_title"):
        col = "company" if kind == "deny_company" else "title"
        pat = feedback.get("pattern")
        if not pat and item and item.get(col):
            pat = "^" + re.escape(str(item[col])) + "$"   # deny exactly the item's company/title
        return kind, {"pattern": _pattern(pat)}
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
        if not -50 <= out.get("percent", 0) <= 100:
            raise FeedbackError("percent must be between -50 and 100")
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


# Which client-origin feedback needs a human confirmation before it applies (ADR-0045 amendment).
def needs_confirmation(kind: str, approval_mode: str) -> bool:
    if kind in ("answer", "profile_answer"):
        return True                                   # never owner-authority answers from a client alone
    return kind == "salary" and approval_mode == "human"   # human mode: pay changes are CLI/confirmed only


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


def _apply_salary(pol: Any, rules: Any, rows: list[dict[str, Any]], problems: list[str],
                  max_multiple: float = 3.0) -> None:
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
    anchors = [v for v in (cfg_floor, cfg_base, base.get("default_ask"), rbase.get("expected")) if v]
    ceiling = int(max(anchors) * max_multiple) if anchors and max_multiple > 0 else None
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
        _clamp(pol, rules, problems, ceiling)   # after every step: never below the floor, never above the ceiling


def _note(problems: list[str], note: str) -> None:
    if note not in problems:
        problems.append(note)


def _clamp(pol: Any, rules: Any, problems: list[str], ceiling: int | None = None) -> None:
    """Asks stay in [floor, ceiling]; the ceiling (intake.salary_max_multiple x the largest configured salary value)
    stops a runaway delta/percent. The configured floor wins over the ceiling."""
    if ceiling is not None:
        for obj, attr in ((pol, "salary_floor"), (pol, "base_salary"), (pol, "default_ask"), (rules, "expected")):
            v = getattr(obj, attr, None) if obj is not None else None
            if v is not None and v > ceiling:
                setattr(obj, attr, ceiling)
                _note(problems, f"salary: {attr} capped at {ceiling} (intake.salary_max_multiple)")
    floor = pol.salary_floor
    if floor is None:
        return
    for obj, attr in ((pol, "default_ask"), (rules, "expected")):
        v = getattr(obj, attr, None) if obj is not None else None
        if v is not None and v < floor:
            setattr(obj, attr, floor)
            _note(problems, f"salary: {attr} raised to the floor {floor}")


def apply_overlays(settings: Any, profile: Any, rows: list[dict[str, Any]]) -> tuple[int | None, list[str]]:
    """Replay structured feedback onto settings.policy, the profile's answers and its salary rule. profile_answer
    rows are not overlays (the runner delivers them to the profile builder). Returns (run_size, problems)."""
    pol, run_size, problems = settings.policy, None, []
    salary = [r for r in rows if r["kind"] == "salary"]
    if salary or id(pol) in _BASE:
        _apply_salary(pol, _salary_rules(profile), salary, problems,
                      getattr(getattr(settings, "intake", None), "salary_max_multiple", 3.0))
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

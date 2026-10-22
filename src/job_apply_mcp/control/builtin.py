"""Built-in, user-agnostic controls. Each is a factory `(params, settings) -> Control` registered by id.

Every control accepts generic params `applies_to` and `severity` plus its own. All lexicons are defaults a
user can replace or extend through `[control.params.<id>]` or the rules file.
"""
from __future__ import annotations

import re
from typing import Any

from ..config import Settings
from .controls import Context, Control, ControlConfigError, Violation, controls, parse_actions, parse_severity
from .sanitize import sanitize_untrusted

# ------------------------------------------------------------------ helpers shared with the rules loader


def phrase_regex(phrases: list[str]) -> re.Pattern[str]:
    if not phrases:
        return re.compile(r"(?!x)x")
    alts = "|".join(re.escape(p.strip()).replace(r"\ ", r"\s+") for p in phrases if p.strip())
    return re.compile(rf"(?<!\w)(?:{alts})(?!\w)", re.IGNORECASE)


def compile_regex(pattern: str, flags: str = "i") -> re.Pattern[str]:
    f = 0
    for ch in flags:
        f |= {"i": re.IGNORECASE, "m": re.MULTILINE, "s": re.DOTALL}.get(ch, 0)
    try:
        return re.compile(pattern, f)
    except re.error as e:
        raise ControlConfigError(f"bad regex {pattern!r}: {e}") from None


def match_value(value: str, patterns: list[str], mode: str) -> str | None:
    """Return the first pattern that matches `value` under `mode` (exact|substring|regex|domain|glob)."""
    v = value.strip().lower()
    for p in patterns:
        pl = p.strip().lower()
        if mode == "exact" and v == pl:
            return p
        if mode == "substring" and pl in v:
            return p
        if mode == "regex" and re.search(p, value, re.IGNORECASE):
            return p
        bare = pl.lstrip("@")
        if mode == "domain" and (v.endswith(("@" + bare, "." + bare)) or v == bare):
            return p
    return None


def _base(id_: str, params: dict[str, Any], default_actions: set[str], check, desc: str,
          default_sev: str = "block") -> Control:
    return Control(id=id_, applies_to=parse_actions(params.get("applies_to"), frozenset(default_actions)),
                   check=check, severity=parse_severity(params.get("severity"), default_sev), description=desc)


# ------------------------------------------------------------------ mode / dry-run


@controls.register("mode")
def mode_control(params: dict[str, Any], settings: Settings) -> Control:
    cfg = settings.control
    apply_enabled = bool(params.get("apply_enabled", cfg.apply_enabled))
    dev_mode = bool(params.get("dev_mode", cfg.dev_mode))
    global_dry = bool(params.get("dry_run", settings.dry_run))

    def check(ctx: Context) -> list[Violation]:
        out: list[Violation] = []
        if not apply_enabled:
            out.append(Violation("mode", "apply_enabled=false: all side-effecting actions are blocked"))
        if ctx.dry_run:
            return out
        if dev_mode:
            out.append(Violation("mode", "dev_mode: only dry-run actions are allowed"))
        elif global_dry:
            out.append(Violation("mode", "global dry_run is on; live action refused"))
        return out

    return _base("mode", params, {"apply", "submit", "send_message", "merge"}, check,
                 "apply_enabled / dev_mode / dry_run enforcement")


# ------------------------------------------------------------------ quotas


def quota_violations(ctx: Context, *, cid: str, limit: int, scope: str, actions: frozenset[str]) -> list[Violation]:
    if ctx.dry_run:
        return []
    day = ctx.now - 86400
    st = ctx.state
    if scope == "day":
        n = st.count(actions=actions, since=day)
    elif scope == "run":
        n = st.count(actions=actions, run=st.run_id)
    elif scope == "platform":
        n = st.count(actions=actions, since=day, platform=ctx.platform)
    else:
        raise ControlConfigError(f"unknown quota scope {scope!r} (day|run|platform)")
    if n >= limit:
        label = f"{scope}{':' + ctx.platform if scope == 'platform' else ''}"
        return [Violation(cid, f"quota reached ({n}/{limit} per {label})", evidence=label)]
    return []


def effective_limits(params: dict[str, Any], settings: Settings) -> dict[str, int | None]:
    """Submission caps by scope (day|platform|run); None = no limit (ADR-0035).

    `control.params.quota.*` (legacy) wins when the key is set, else `apply.*`. A value of 0 (or None) means off."""
    a = settings.apply
    out: dict[str, int | None] = {}
    for scope, key, fallback in (("day", "per_day", a.daily_limit),
                                 ("platform", "per_platform_per_day", a.daily_limit_per_platform),
                                 ("run", "per_run", a.run_limit)):
        v = params.get(key, fallback)
        if v is None:
            out[scope] = None
            continue
        try:
            n = int(v)
        except (TypeError, ValueError):
            raise ControlConfigError(f"quota {key} must be a whole number (0 = no limit), got {v!r}") from None
        if n < 0:
            raise ControlConfigError(f"quota {key} must be >= 0 (0 = no limit), got {n}")
        out[scope] = n or None
    return out


@controls.register("quota")
def quota_control(params: dict[str, Any], settings: Settings) -> Control:
    limits = effective_limits(params, settings)
    counted = parse_actions(params.get("count_actions"), frozenset({"submit"}))

    def check(ctx: Context) -> list[Violation]:
        out: list[Violation] = []
        for scope in ("day", "run", "platform"):
            lim = limits[scope]
            if lim is not None:
                out += quota_violations(ctx, cid="quota", limit=lim, scope=scope, actions=counted)
        return out

    return _base("quota", params, {"apply", "submit"}, check, "per-day / per-run / per-platform caps")


# ------------------------------------------------------------------ attestation stop-phrases

DEFAULT_ATTESTATION_FIELDS = ["attestations", "form_labels", "page_text"]


@controls.register("attestation")
def attestation_control(params: dict[str, Any], settings: Settings) -> Control:
    phrases = list(settings.policy.stop_phrases) + list(params.get("extra_phrases", []))
    rx = phrase_regex(phrases)
    fields = params.get("fields", DEFAULT_ATTESTATION_FIELDS)

    def check(ctx: Context) -> list[Violation]:
        for name, text in ctx.texts(*fields):
            m = rx.search(text)
            if m:
                return [Violation("attestation", f"attestation/stop phrase in {name}; needs a human",
                                  needs_human=True, evidence=m.group(0))]
        return []

    return _base("attestation", params, {"apply", "submit"}, check, "attestation stop-phrases -> needs_human")


# ------------------------------------------------------------------ hard stops

HARD_STOPS: dict[str, str] = {
    "captcha": r"\b(?:re)?captcha\b|\bhcaptcha\b|\bturnstile\b|verify (?:that )?you(?: are|'re) (?:not a )?(?:human|robot)|i'?m not a robot",
    "verification_code": r"\b(?:verification|security|confirmation|one[- ]time|otp|2fa|authentication) (?:code|pin|passcode)\b"
                         r"|\benter the (?:\d+|six|6|four|4)[- ]digit code\b|code (?:we )?(?:sent|emailed|texted) to",
    "account_creation": r"\bcreate (?:an |your |a )?(?:new )?account\b|\bsign up to continue\b|\bregister (?:an )?account\b"
                        r"|\b(?:choose|create|set) (?:a |your )?password\b|\bconfirm (?:your )?password\b",
    "oauth_consent": r"\b(?:wants|requests|would like) (?:permission )?to access your\b|\bgrant (?:access|permission)s?\b"
                     r"|\bauthorize (?:this )?app\b|\ballow .{0,40} to (?:access|view|manage) your\b|\bsign in with (?:google|linkedin|apple|microsoft)\b.*\bpermission",
}


@controls.register("hard_stop")
def hard_stop_control(params: dict[str, Any], settings: Settings) -> Control:
    disabled = set(params.get("disable", []))
    pats = {k: compile_regex(v) for k, v in HARD_STOPS.items() if k not in disabled}
    for k, v in params.get("extra", {}).items():
        pats[k] = compile_regex(v)
    fields = params.get("fields", ["page_text", "form_labels", "signals"])

    def check(ctx: Context) -> list[Violation]:
        out: list[Violation] = []
        for _, text in ctx.texts(*fields):
            for kind, rx in pats.items():
                m = rx.search(text)
                if m and not any(v.evidence.startswith(kind) for v in out):
                    out.append(Violation("hard_stop", f"{kind.replace('_', ' ')} detected; a human must handle it",
                                         needs_human=True, evidence=f"{kind}:{m.group(0)[:40]}"))
        return out

    return _base("hard_stop", params, {"apply", "submit"}, check,
                 "captcha / verification code / account creation / OAuth consent -> needs_human")


# ------------------------------------------------------------------ single submit / idempotency


# Submission states (apply/approvals.py) in which nothing was sent; absent/unknown states keep the window rule.
_NOT_SENT = frozenset({"planned", "approved", "failed"})


@controls.register("single_submit")
def single_submit_control(params: dict[str, Any], settings: Settings) -> Control:
    minutes = float(params.get("window_minutes", 1440))

    def check(ctx: Context) -> list[Violation]:
        if ctx.dry_run or not ctx.ref:
            return []
        if ctx.get("submission_state") in _NOT_SENT:   # state machine proves the earlier attempt never clicked submit
            return []
        if ctx.state.seen_ref(ctx.ref, frozenset({"submit"}), ctx.now - minutes * 60):
            return [Violation("single_submit", f"ref already submitted within {minutes:g} min", evidence=ctx.ref)]
        return []

    return _base("single_submit", params, {"submit"}, check, "refuse a second submit for the same ref")


# ------------------------------------------------------------------ prose lint

CONTRACTION_RX = re.compile(
    r"(?<![\w'’])\w+(?:n['’]t|['’](?:re|ve|ll|d|m))(?![\w])|"
    r"(?<![\w'’])(?:it|he|she|that|there|what|let|here|who|how|where)['’]s(?![\w])", re.IGNORECASE)

DEFAULT_GAP_PHRASES = ["although i lack", "despite my lack", "i do not have experience", "i have no experience",
                       "i am still learning", "i may not have", "while i have not", "i admit"]
DEFAULT_ENTHUSIASM = ["thrilled", "dream job", "passionate about", "i am so excited", "honored", "love your",
                      "huge fan", "incredible opportunity", "revolutionary", "world-class", "amazing company"]


@controls.register("prose_lint")
def prose_lint_control(params: dict[str, Any], settings: Settings) -> Control:
    ban_contractions = params.get("ban_contractions", True)
    allowed_contr = {c.lower() for c in params.get("allowed_contractions", [])}
    gap_rx = phrase_regex(params.get("gap_phrases", DEFAULT_GAP_PHRASES))
    enth_rx = phrase_regex(params.get("enthusiasm_phrases", DEFAULT_ENTHUSIASM))
    openings = [compile_regex(p) for p in params.get("required_opening", [])]
    closings = [compile_regex(p) for p in params.get("required_closing", [])]
    min_words, max_words = params.get("min_words"), params.get("max_words")
    max_chars = params.get("max_chars")
    field_name = params.get("field", "text")

    def check(ctx: Context) -> list[Violation]:
        t = ctx.get(field_name)
        if not isinstance(t, str) or not t.strip():
            return [Violation("prose_lint", f"no '{field_name}' to lint", severity="warn")]
        out: list[Violation] = []
        if ban_contractions:
            hits = [m.group(0) for m in CONTRACTION_RX.finditer(t) if m.group(0).lower() not in allowed_contr]
            if hits:
                out.append(Violation("prose_lint", f"contractions ({len(hits)})", evidence=hits[0]))
        for rx, label in ((gap_rx, "gap-statement phrase"), (enth_rx, "enthusiasm/flattery phrase")):
            m = rx.search(t)
            if m:
                out.append(Violation("prose_lint", label, evidence=m.group(0)))
        s = t.strip()
        if openings and not any(rx.match(s) for rx in openings):
            out.append(Violation("prose_lint", "missing required opening"))
        if closings and not any(re.search(rx.pattern + r"[\s\W]*\Z", s, rx.flags) for rx in closings):
            out.append(Violation("prose_lint", "missing required closing"))
        words = len(s.split())
        if min_words is not None and words < int(min_words):
            out.append(Violation("prose_lint", f"too short ({words}<{min_words} words)"))
        if max_words is not None and words > int(max_words):
            out.append(Violation("prose_lint", f"too long ({words}>{max_words} words)"))
        if max_chars is not None and len(s) > int(max_chars):
            out.append(Violation("prose_lint", f"too long ({len(s)}>{max_chars} chars)"))
        return out

    return _base("prose_lint", params, {"write_prose"}, check,
                 "contractions, gap/enthusiasm lexicons, opening/closing templates, length")


# ------------------------------------------------------------------ recipient deny-list


@controls.register("recipient_deny")
def recipient_deny_control(params: dict[str, Any], settings: Settings) -> Control:
    deny = list(params.get("deny", []))
    allow = list(params.get("allow", []))
    mode = params.get("match", "domain")
    field_name = params.get("field", "recipient")

    def check(ctx: Context) -> list[Violation]:
        raw = ctx.get(field_name)
        recips = raw if isinstance(raw, list) else ([raw] if raw else [])
        if not recips:
            return [Violation("recipient_deny", "message has no recipient")]
        out: list[Violation] = []
        for r in map(str, recips):
            hit = match_value(r, deny, mode)
            if hit:
                out.append(Violation("recipient_deny", "recipient on deny-list", evidence=hit))
            elif allow and not match_value(r, allow, mode):
                out.append(Violation("recipient_deny", "recipient not on allow-list", evidence=r[:40]))
        return out

    return _base("recipient_deny", params, {"send_message"}, check, "recipient deny/allow list")


# ------------------------------------------------------------------ page text addressed to AI


@controls.register("page_text_injection")
def injection_control(params: dict[str, Any], settings: Settings) -> Control:
    fields = params.get("fields", ["page_text", "description"])
    extra = params.get("extra_patterns", [])

    def check(ctx: Context) -> list[Violation]:
        out: list[Violation] = []
        for name, text in ctx.texts(*fields):
            res = sanitize_untrusted(text, extra_patterns=extra)
            if res.flagged:
                out.append(Violation("page_text_injection",
                                     f"{name} contains text addressed to an AI ({len(res.removed)} span(s)); "
                                     "treat as data and sanitize before any LLM call",
                                     evidence=res.removed[0]))
        return out

    return _base("page_text_injection", params, {"apply", "submit", "write_prose"}, check,
                 "flags instructions found in scraped content", default_sev="warn")

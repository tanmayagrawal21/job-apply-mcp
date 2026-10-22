"""Declarative user rules (TOML) -> Controls. The file lives outside the repo (path in `[control] rules_file`).

Top-level tables:
  [mode]            apply_enabled / dev_mode overrides
  [params.<id>]     per-built-in-control param overrides (merged over config)
  [[rule]]          user rules; common keys: id, type, applies_to, severity (block|warn), message, needs_human
Rule types: phrases_banned, phrases_required, regex, threshold, allow_list, deny_list, quota.
"""
from __future__ import annotations

import tomllib
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .builtin import compile_regex, match_value, phrase_regex, quota_violations
from .controls import Context, Control, ControlConfigError, Violation, parse_actions, parse_severity


@dataclass
class RuleSet:
    mode: dict[str, Any] = field(default_factory=dict)
    params: dict[str, dict[str, Any]] = field(default_factory=dict)
    controls: list[Control] = field(default_factory=list)


def _need(rule: dict, key: str, rid: str) -> Any:
    if key not in rule:
        raise ControlConfigError(f"rule '{rid}': missing '{key}'")
    return rule[key]


def _text_values(ctx: Context, field_name: str) -> list[str]:
    return [t for _, t in ctx.texts(field_name)]


def build_rule(rule: dict[str, Any]) -> Control:
    rid = str(rule.get("id") or "")
    if not rid:
        raise ControlConfigError("every [[rule]] needs an 'id'")
    rtype = _need(rule, "type", rid)
    applies = parse_actions(rule.get("applies_to"))
    severity = parse_severity(rule.get("severity"))
    human = bool(rule.get("needs_human", False))
    custom = rule.get("message")
    fld = rule.get("field", "text")

    def v(default_msg: str, evidence: str = "") -> Violation:
        return Violation(rid, str(custom or default_msg), needs_human=human, evidence=evidence)

    check = None
    if rtype in ("phrases_banned", "phrases_required"):
        phrases = list(_need(rule, "phrases", rid))
        rx = phrase_regex(phrases)
        if rtype == "phrases_banned":
            def check(ctx: Context) -> list[Violation]:
                for t in _text_values(ctx, fld):
                    m = rx.search(t)
                    if m:
                        return [v("banned phrase", m.group(0))]
                return []
        else:
            mode_all = rule.get("require", "any") == "all"

            def check(ctx: Context) -> list[Violation]:
                ts = " ".join(_text_values(ctx, fld))
                if not ts:
                    return [v(f"required phrase check: no '{fld}'")]
                found = {m.group(0).lower() for m in rx.finditer(ts)}
                ok = (all(any(p.lower() == f for f in found) for p in phrases) if mode_all else bool(found))
                return [] if ok else [v("required phrase missing")]
    elif rtype == "regex":
        rx = compile_regex(str(_need(rule, "pattern", rid)), str(rule.get("flags", "i")))
        must = bool(rule.get("must_match", False))

        def check(ctx: Context) -> list[Violation]:
            ts = _text_values(ctx, fld)
            if not ts:
                return [] if not must else [v(f"pattern required but no '{fld}'")]
            hit = next((m for t in ts if (m := rx.search(t))), None)
            if must:
                return [] if hit else [v("required pattern not found")]
            return [v("forbidden pattern matched", hit.group(0))] if hit else []
    elif rtype == "threshold":
        lo, hi = rule.get("min"), rule.get("max")
        if lo is None and hi is None:
            raise ControlConfigError(f"rule '{rid}': threshold needs min and/or max")
        measure = rule.get("measure", "value")

        def check(ctx: Context) -> list[Violation]:
            raw = ctx.get(fld)
            if raw is None:
                return [v(f"'{fld}' missing for threshold")]
            try:
                n = float(len(str(raw).split()) if measure == "words" else
                          len(str(raw)) if measure == "chars" else raw)
            except (TypeError, ValueError):
                return [v(f"'{fld}' is not numeric")]
            if lo is not None and n < float(lo):
                return [v(f"{fld} {n:g} below min {lo}")]
            if hi is not None and n > float(hi):
                return [v(f"{fld} {n:g} above max {hi}")]
            return []
    elif rtype in ("allow_list", "deny_list"):
        values = [str(x) for x in _need(rule, "values", rid)]
        mode = rule.get("match", "exact")
        if mode not in ("exact", "substring", "regex", "domain"):
            raise ControlConfigError(f"rule '{rid}': match must be exact|substring|regex|domain")
        fld = rule.get("field", "company")
        deny = rtype == "deny_list"

        def check(ctx: Context) -> list[Violation]:
            raw = ctx.get(fld)
            items = raw if isinstance(raw, list) else ([raw] if raw else [])
            if not items:
                return [] if deny else [v(f"'{fld}' missing; not on allow-list")]
            out = []
            for it in map(str, items):
                hit = match_value(it, values, mode)
                if deny and hit:
                    out.append(v(f"{fld} on deny-list", hit))
                elif not deny and not hit:
                    out.append(v(f"{fld} not on allow-list", it[:40]))
            return out
    elif rtype == "quota":
        limit = int(_need(rule, "limit", rid))
        scope = rule.get("scope", "day")
        counted = parse_actions(rule.get("count_actions"), frozenset({"submit"}))
        if scope not in ("day", "run", "platform"):
            raise ControlConfigError(f"rule '{rid}': scope must be day|run|platform")

        def check(ctx: Context) -> list[Violation]:
            out = quota_violations(ctx, cid=rid, limit=limit, scope=scope, actions=counted)
            if custom:
                for x in out:
                    x.message = str(custom)
            for x in out:
                x.needs_human = human
            return out
    else:
        raise ControlConfigError(f"rule '{rid}': unknown type {rtype!r}")
    return Control(id=rid, applies_to=applies, check=check, severity=severity,
                   description=str(rule.get("description", rtype)))


def parse_rules(data: dict[str, Any]) -> RuleSet:
    known = {"mode", "params", "rule"}
    extra = set(data) - known
    if extra:
        raise ControlConfigError(f"unknown top-level key(s) {sorted(extra)}; expected {sorted(known)}")
    rs = RuleSet(mode=dict(data.get("mode", {})), params={k: dict(v) for k, v in data.get("params", {}).items()})
    seen: set[str] = set()
    for r in data.get("rule", []):
        c = build_rule(r)
        if c.id in seen:
            raise ControlConfigError(f"duplicate rule id '{c.id}'")
        seen.add(c.id)
        rs.controls.append(c)
    return rs


def load_rules(path: str | Path) -> RuleSet:
    p = Path(path).expanduser()
    if not p.is_file():
        raise ControlConfigError(f"rules file not found: {p}")
    try:
        data = tomllib.loads(p.read_text(encoding="utf-8"))
    except tomllib.TOMLDecodeError as e:
        raise ControlConfigError(f"rules file {p}: {e}") from None
    return parse_rules(data)

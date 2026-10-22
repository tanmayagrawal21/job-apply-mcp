"""Control primitives: Violation, Context, UsageState, Control and the control registry (ADR-0014)."""
from __future__ import annotations

import time
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

from ..core.registry import Registry

ACTION_KINDS = frozenset({"apply", "submit", "send_message", "write_prose", "merge"})
SIDE_EFFECTING = frozenset({"apply", "submit", "send_message", "merge"})
RELEASE = "submit_release"   # audit/usage action that cancels an earlier `submit` for the same ref (ADR-0032)
SEVERITIES = ("block", "warn")


class ControlConfigError(ValueError):
    """Raised for invalid control configuration or rules files."""


@dataclass
class Violation:
    control: str
    message: str
    severity: str = "block"          # block | warn
    needs_human: bool = False        # a human must decide; the agent must not retry or work around it
    evidence: str = ""

    def compact(self) -> dict[str, Any]:
        d: dict[str, Any] = {"c": self.control, "m": self.message[:160]}
        if self.needs_human:
            d["h"] = 1
        if self.evidence:
            d["e"] = self.evidence[:80]
        return d


@dataclass
class UsageEvent:
    ts: float
    action: str
    ref: str = ""
    platform: str = ""
    company: str = ""
    run: str = ""


class UsageState:
    """Counts of live (non-dry-run) actions that passed their checks. Rebuilt from the audit log at start."""

    def __init__(self, run_id: str = "") -> None:
        self.run_id = run_id
        self.events: list[UsageEvent] = []

    def add(self, ev: UsageEvent) -> None:
        self.events.append(ev)

    def _live(self, action_filter: set[str] | frozenset[str]) -> list[UsageEvent]:
        """Events of the given actions, minus any `submit` that a later `submit_release` for the same ref cancels."""
        released: set[str] = set()
        out: list[UsageEvent] = []
        for e in reversed(self.events):
            if e.action == RELEASE:
                if e.ref:
                    released.add(e.ref)
            elif e.action == "submit" and e.ref and e.ref in released:
                continue
            if e.action in action_filter:
                out.append(e)
        return out

    def count(self, *, actions: set[str] | frozenset[str], since: float = 0.0, run: str | None = None,
              platform: str | None = None) -> int:
        return sum(1 for e in self._live(actions)
                   if e.ts >= since and (run is None or e.run == run)
                   and (platform is None or e.platform == platform))

    def seen_ref(self, ref: str, actions: set[str] | frozenset[str], since: float) -> bool:
        return any(e.ref == ref and e.ts >= since for e in self._live(actions))


@dataclass
class Context:
    action: str
    data: dict[str, Any] = field(default_factory=dict)
    state: UsageState = field(default_factory=UsageState)
    now: float = field(default_factory=time.time)

    def get(self, path: str, default: Any = None) -> Any:
        cur: Any = self.data
        for part in path.split("."):
            if isinstance(cur, dict) and part in cur:
                cur = cur[part]
            else:
                return default
        return cur

    @property
    def dry_run(self) -> bool:
        return bool(self.data.get("dry_run", True))

    @property
    def ref(self) -> str:
        return str(self.data.get("ref") or "")

    @property
    def platform(self) -> str:
        return str(self.data.get("platform") or "")

    def texts(self, *names: str) -> list[tuple[str, str]]:
        """(field, text) pairs for the named fields; list values are flattened."""
        out: list[tuple[str, str]] = []
        for n in names:
            v = self.get(n)
            if isinstance(v, str) and v:
                out.append((n, v))
            elif isinstance(v, list):
                out.extend((n, str(x)) for x in v if x)
        return out


CheckFn = Callable[[Context], list[Violation]]


@dataclass
class Control:
    id: str
    applies_to: frozenset[str]
    check: CheckFn
    severity: str = "block"
    description: str = ""

    def run(self, ctx: Context) -> list[Violation]:
        if ctx.action not in self.applies_to:
            return []
        out = self.check(ctx)
        for v in out:
            if self.severity == "warn":
                v.severity = "warn"
        return out


def parse_actions(value: Any, default: frozenset[str] | None = None) -> frozenset[str]:
    if value is None:
        return default if default is not None else ACTION_KINDS
    if isinstance(value, str):
        value = [value]
    bad = set(value) - ACTION_KINDS
    if bad:
        raise ControlConfigError(f"unknown action kind(s) {sorted(bad)}; known: {sorted(ACTION_KINDS)}")
    return frozenset(value)


def parse_severity(value: Any, default: str = "block") -> str:
    v = default if value is None else value
    if v not in SEVERITIES:
        raise ControlConfigError(f"severity must be one of {SEVERITIES}, got {v!r}")
    return v


# Built-in control factories: factory(params: dict, settings) -> Control
controls: Registry = Registry("control")

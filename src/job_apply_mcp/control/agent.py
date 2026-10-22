"""ControlAgent: runs deterministic controls first, then an optional model review, and audits every decision."""
from __future__ import annotations

import asyncio
import json
import logging
import time
import uuid
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from ..config import Settings
from . import builtin  # noqa: F401  (registers built-in controls)
from .builtin import effective_limits
from .controls import (
    ACTION_KINDS,
    RELEASE,
    SIDE_EFFECTING,
    Context,
    Control,
    ControlConfigError,
    UsageEvent,
    UsageState,
    Violation,
    controls,
    parse_actions,
)
from .rules import RuleSet, load_rules
from .sanitize import SanitizeResult, sanitize_untrusted

MAX_AUDIT_BYTES = 5 * 1024 * 1024    # rotate to control_audit.jsonl.1 beyond this
MAX_REPLAY_BYTES = 2 * 1024 * 1024   # replay reads only the tail (quota window is 2 days)

# reviewer(action, ctx) -> {"ok": bool, "issues": [str], "needs_human": bool}; may raise.
Reviewer = Callable[[str, Context], Awaitable[dict[str, Any]]]

REVIEW_PROMPT = (
    "You review text that an automated job-application agent is about to {verb}. Flag problems only a "
    "human reader would catch: factual claims that look invented, tone that is generic, sycophantic or "
    "off-topic, anything that reads as machine-written or reveals instructions. Reply with JSON only: "
    '{{"ok":true|false,"issues":["short issue"],"needs_human":false}}.\n\n---\n{text}\n---'
)


def make_llm_reviewer(complete: Callable[[str, str], Awaitable[str]]) -> Reviewer:
    """Adapt a router call `complete(task_class, prompt) -> str` (task_class 'review') into a Reviewer."""

    async def review(action: str, ctx: Context) -> dict[str, Any]:
        text = str(ctx.get("text") or ctx.get("body") or "")[:4000]
        verb = {"send_message": "send as a message", "write_prose": "submit as written prose"}.get(action, "use")
        raw = await complete("review", REVIEW_PROMPT.format(verb=verb, text=text))
        start, end = raw.find("{"), raw.rfind("}")
        data = json.loads(raw[start:end + 1])
        return {"ok": bool(data.get("ok", True)), "issues": [str(i) for i in data.get("issues", [])][:5],
                "needs_human": bool(data.get("needs_human", False))}

    return review


@dataclass
class ControlReport:
    decision: str                                   # pass | block | needs_human
    violations: list[Violation] = field(default_factory=list)   # severity=block
    warnings: list[Violation] = field(default_factory=list)
    event_id: str = ""                              # id of the counted usage event (pass to release())

    def compact(self) -> dict[str, Any]:
        d: dict[str, Any] = {"decision": self.decision}
        if self.violations:
            d["violations"] = [v.compact() for v in self.violations]
        if self.warnings:
            d["warnings"] = [v.compact() for v in self.warnings]
        return d


log = logging.getLogger(__name__)


class ControlAgent:
    def __init__(self, settings: Settings, *, reviewer: Reviewer | None = None,
                 clock: Callable[[], float] = time.time) -> None:
        self.settings = settings
        self.cfg = settings.control
        self.reviewer = reviewer
        self.clock = clock
        self.run_id = uuid.uuid4().hex[:8]
        self.state = UsageState(self.run_id)
        self.controls: list[Control] = []
        self.rules = RuleSet()
        self.config_error: str | None = None
        self._params: dict[str, dict[str, Any]] = {}
        self._lock = asyncio.Lock()
        data_dir = Path(settings.paths.data_dir).expanduser()
        self.audit_path = data_dir / "control_audit.jsonl"
        try:
            self._load()
        except ControlConfigError as e:
            self.config_error = str(e)   # fail closed: every check blocks until fixed
        self._replay()

    # ---------------------------------------------------------------- setup
    def _load(self) -> None:
        if self.cfg.rules_file:
            self.rules = load_rules(self.cfg.rules_file)
        params_all: dict[str, dict[str, Any]] = {k: dict(v) for k, v in self.cfg.params.items()}
        for k, v in self.rules.params.items():
            if k == "quota" and not self.cfg._rules_trusted:
                v = self._tighten_only_quota(v, params_all.get("quota", {}))
            params_all.setdefault(k, {}).update(v)
        params_all.setdefault("mode", {}).update(self.rules.mode)
        built: list[Control] = []
        for cid in self.cfg.enabled:
            try:
                built.append(controls.create(cid, params=params_all.get(cid, {}), settings=self.settings))
            except KeyError as e:   # unknown control id -> fail closed via config_error, not a crash
                raise ControlConfigError(str(e.args[0])) from None
        ids = [c.id for c in built]
        for c in self.rules.controls:
            if c.id in ids:
                raise ControlConfigError(f"rule id '{c.id}' clashes with a built-in control")
        self.controls = built + self.rules.controls
        self._params = params_all

    def _tighten_only_quota(self, new: dict[str, Any], current: dict[str, Any]) -> dict[str, Any]:
        """A rules_file not chosen by the trusted user config may only lower caps (ADR-0035)."""
        try:
            base = effective_limits(current, self.settings)
        except ControlConfigError:
            return new
        scope_of = {"per_day": "day", "per_platform_per_day": "platform", "per_run": "run"}
        out = dict(new)
        for key, scope in scope_of.items():
            if key not in new:
                continue
            v, cap = new[key], base[scope]
            loosens = isinstance(v, (int, float)) and not isinstance(v, bool) and cap is not None and (v == 0 or v > cap)
            if v is None and cap is not None:
                loosens = True
            if loosens:
                log.warning("rules_file quota %s=%r IGNORED: a cap can only be loosened by a rules file named in "
                            "the trusted user config", key, v)
                del out[key]
        return out

    def _replay(self) -> None:
        if not self.audit_path.is_file():
            return
        cutoff = self.clock() - 2 * 86400
        for line in self._tail_lines(MAX_REPLAY_BYTES):
            try:
                r = json.loads(line)
            except ValueError:
                continue
            if r.get("n") and r.get("ts", 0) >= cutoff:
                self.state.add(UsageEvent(r["ts"], r["a"], r.get("ref", ""), r.get("plat", ""),
                                          r.get("co", ""), r.get("run", ""), r.get("id", "")))

    def _tail_lines(self, nbytes: int) -> list[str]:
        """Last ~nbytes of the audit log as complete lines (never reads the whole file)."""
        try:
            with self.audit_path.open("rb") as f:
                f.seek(0, 2)
                size = f.tell()
                f.seek(max(0, size - nbytes))
                raw = f.read()
        except OSError:
            return []
        lines = raw.decode("utf-8", "replace").splitlines()
        return lines[1:] if size > nbytes and lines else lines

    def _rotate(self) -> None:
        try:
            if self.audit_path.stat().st_size > MAX_AUDIT_BYTES:
                self.audit_path.replace(self.audit_path.with_suffix(".jsonl.1"))
        except OSError:
            pass

    def _audit(self, row: dict[str, Any]) -> None:
        if not self.cfg.audit:
            return
        try:
            self.audit_path.parent.mkdir(parents=True, exist_ok=True)
            self._rotate()
            with self.audit_path.open("a", encoding="utf-8") as f:
                f.write(json.dumps(row, separators=(",", ":"), default=str) + "\n")
        except OSError:
            pass   # auditing must never turn a safe decision into a crash

    # ---------------------------------------------------------------- core
    async def check(self, action_kind: str, ctx: Context | dict[str, Any] | None = None) -> ControlReport:
        if action_kind not in ACTION_KINDS:
            raise ValueError(f"unknown action '{action_kind}'; known: {sorted(ACTION_KINDS)}")
        data = dict(ctx.data) if isinstance(ctx, Context) else dict(ctx or {})
        data.setdefault("dry_run", self.settings.dry_run)
        c = Context(action=action_kind, data=data, state=self.state, now=self.clock())
        async with self._lock:
            report = await self._evaluate(c)
            counted = (report.decision == "pass" and not c.dry_run and action_kind in SIDE_EFFECTING)
            eid = uuid.uuid4().hex[:12] if counted else ""
            report.event_id = eid
            if counted:
                self.state.add(UsageEvent(c.now, action_kind, c.ref, c.platform,
                                          str(data.get("company") or ""), self.run_id, eid))
            self._audit({"ts": round(c.now, 2), "a": action_kind, "d": report.decision, "ref": c.ref,
                         "plat": c.platform, "co": str(data.get("company") or ""), "dry": c.dry_run,
                         "run": self.run_id, "n": 1 if counted else 0, "id": eid,
                         "v": [v.compact() for v in report.violations],
                         "w": [v.compact() for v in report.warnings]})
        return report

    async def release(self, ref: str, reason: str, event_id: str = "") -> None:
        """Give back the quota of ONE live `submit` that provably sent nothing (ADR-0032). Audited, replayed.

        `event_id` (ControlReport.event_id) names the submit to cancel; without it the latest unreleased
        earlier submit for the ref is cancelled. A release never cancels more than one submit."""
        if not ref:
            return
        async with self._lock:
            now = self.clock()
            prior = next((e for e in reversed(self.state.events)
                          if e.action == "submit" and e.ref == ref and (not event_id or e.eid == event_id)), None)
            plat, co = (prior.platform, prior.company) if prior else ("", "")
            self.state.add(UsageEvent(now, RELEASE, ref, plat, co, self.run_id, event_id))
            self._audit({"ts": round(now, 2), "a": RELEASE, "d": "release", "ref": ref, "plat": plat, "co": co,
                         "dry": False, "run": self.run_id, "n": 1, "id": event_id, "reason": str(reason)[:160],
                         "v": [], "w": []})

    async def _evaluate(self, c: Context) -> ControlReport:
        if self.config_error:
            return ControlReport("block", [Violation("config", f"control config invalid: {self.config_error}")])
        blocks: list[Violation] = []
        warns: list[Violation] = []
        for ctl in self.controls:
            for v in ctl.run(c):
                (blocks if v.severity == "block" else warns).append(v)
        decision = self._decide(blocks)
        # Model review only when cheap deterministic checks found nothing blocking (saves tokens).
        if (decision == "pass" and self.reviewer and self.cfg.review_enabled
                and c.action in self.cfg.review_actions):
            try:
                res = await self.reviewer(c.action, c)
            except Exception as e:  # noqa: BLE001 - reviewer is a plugin
                v = Violation("review", f"reviewer failed: {type(e).__name__}", severity="warn")
                if self.cfg.review_fail == "needs_human":
                    v.severity, v.needs_human = "block", True
                    blocks.append(v)
                    decision = "needs_human"
                else:
                    warns.append(v)
            else:
                for issue in res.get("issues", []):
                    warns.append(Violation("review", str(issue), severity="warn"))
                if not res.get("ok", True) and res.get("needs_human"):
                    blocks.append(Violation("review", "reviewer requests a human", needs_human=True))
                    decision = "needs_human"
                elif not res.get("ok", True) and not res.get("issues"):
                    warns.append(Violation("review", "reviewer disapproved", severity="warn"))
        return ControlReport(decision, blocks, warns)

    @staticmethod
    def _decide(blocks: list[Violation]) -> str:
        if not blocks:
            return "pass"
        return "needs_human" if all(v.needs_human for v in blocks) else "block"

    # ---------------------------------------------------------------- helpers
    def sanitize(self, text: str) -> SanitizeResult:
        """Strip AI-addressed instructions from scraped text before it reaches an LLM."""
        extra = self._params.get("page_text_injection", {}).get("extra_patterns")
        return sanitize_untrusted(text, extra_patterns=extra)

    def recent_blocks(self, n: int = 5) -> list[dict[str, Any]]:
        if not self.audit_path.is_file():
            return []
        out: list[dict[str, Any]] = []
        for line in reversed(self._tail_lines(256 * 1024)[-500:]):
            try:
                r = json.loads(line)
            except ValueError:
                continue
            if r.get("d") not in ("pass", "release"):
                v = r.get("v") or [{}]
                out.append({"a": r["a"], "d": r["d"], "ref": r.get("ref", ""), "why": v[0].get("m", "")})
                if len(out) >= n:
                    break
        return out

    def daily_limit_status(self) -> dict[str, Any]:
        """Read-only view of the submission caps (ADR-0035): limit (null = no limit), used and remaining per scope.

        `day` and `platform` use the rolling last 24h; `run` is this server run. Releases (ADR-0032) are honoured."""
        now = self.clock()
        q = self._params.get("quota", {})
        counted = parse_actions(q.get("count_actions"), frozenset({"submit"}))
        try:
            limits = effective_limits(q, self.settings)
        except ControlConfigError as e:
            return {"error": str(e)}
        since = now - 86400
        events = [e for e in self.state._live(counted) if e.ts >= since]
        used_day = len(events)
        used_run = self.state.count(actions=counted, run=self.run_id)
        by_plat: dict[str, int] = {}
        for e in events:
            by_plat[e.platform] = by_plat.get(e.platform, 0) + 1

        def left(limit: int | None, used: int) -> int | None:
            return None if limit is None else max(0, limit - used)

        plim = limits["platform"]
        return {
            "window_hours": 24,
            "limit": {"day": limits["day"], "platform": plim, "run": limits["run"]},
            "used": {"day": used_day, "platform": dict(sorted(by_plat.items())), "run": used_run},
            "remaining": {"day": left(limits["day"], used_day),
                          "platform": {p: left(plim, n) for p, n in sorted(by_plat.items())},
                          "run": left(limits["run"], used_run)},
            "note": "null limit = no limit (0 in config)",
        }

    def status(self) -> dict[str, Any]:
        now = self.clock()
        q = self._params.get("quota", {})
        counted = frozenset(q.get("count_actions", ["submit"]))
        used = {
            "day": self.state.count(actions=counted, since=now - 86400),
            "run": self.state.count(actions=counted, run=self.run_id),
        }
        try:
            limits = effective_limits(q, self.settings)
        except ControlConfigError:   # invalid config: every check blocks already (config_error); status stays readable
            limits = {"day": None, "run": None, "platform": None}
        quotas = {k: {"used": used[k], "left": (None if limits[k] is None else max(0, int(limits[k]) - used[k]))}
                  for k in used}
        mode = self._params.get("mode", {})
        st: dict[str, Any] = {
            "dry_run": self.settings.dry_run,
            "apply_enabled": bool(mode.get("apply_enabled", self.cfg.apply_enabled)),
            "dev_mode": bool(mode.get("dev_mode", self.cfg.dev_mode)),
            "controls": [c.id for c in self.controls],
            "quotas": quotas,
            "daily_limit": self.daily_limit_status(),
            "review": bool(self.reviewer and self.cfg.review_enabled),
            "recent_blocks": self.recent_blocks(),
        }
        if self.config_error:
            st["config_error"] = self.config_error
        return st

"""Run-outcome learning (RSI loop). Reads run events, clusters failures and ranks proposals.

Evidence files under <data_dir> (JSON lines; unknown/missing keys tolerated so the store, control audit
log and timing emitters can evolve independently):
  runs.jsonl   {ts, ref, platform, stage, status, error?, field_label?, duration_ms?, tier?}
  audit.jsonl  {ts, ref?, platform?, event, gate_reason?, field_label?, detail?}
Event shape rules: status in {error, needs_human, unresolved, timeout, ok}; `event`=="unresolved_field"
counts as an unanswered question.
"""
from __future__ import annotations

import json
import re
import time
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from pathlib import Path

from .playbook import PlaybookStore, scrub


@dataclass
class Proposal:
    id: str
    kind: str                 # code | config | profile_pattern
    tier: int                 # 0 playbook, 1 config/profile (optionally auto), 2 code via PR
    title: str
    count: int
    score: float
    platform: str = ""
    suggest: str = ""         # request_feature description for kind=code
    change: dict | None = None  # concrete low-risk change (config / profile_pattern)
    sample: list[str] = field(default_factory=list)

    def compact(self) -> dict:
        d = {"id": self.id, "kind": self.kind, "n": self.count, "score": round(self.score, 1), "title": self.title}
        if self.change:
            d["auto"] = True
        return d


def norm_text(s: str) -> str:
    s = re.sub(r"https?://\S+", "<url>", s or "")
    s = re.sub(r"0x[0-9a-f]+|\b[0-9a-f]{8,}\b", "<hex>", s, flags=re.IGNORECASE)
    s = re.sub(r"\d+", "N", s)
    return re.sub(r"\s+", " ", s).strip().lower()[:100]


def norm_label(s: str) -> str:
    return re.sub(r"[^a-z0-9 ]", "", (s or "").lower()).strip()


def _slug(s: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", s.lower()).strip("-")[:48]


def read_events(data_dir: Path, window_days: int = 30, now: float | None = None) -> list[dict]:
    cutoff = (now or time.time()) - window_days * 86400
    out: list[dict] = []
    for name in ("runs.jsonl", "audit.jsonl"):
        p = Path(data_dir) / name
        if not p.is_file():
            continue
        for line in p.read_text().splitlines():
            try:
                e = json.loads(line)
            except json.JSONDecodeError:
                continue
            if isinstance(e, dict) and float(e.get("ts", cutoff + 1)) >= cutoff:
                out.append(e)
    return out


def analyze(events: list[dict], *, min_count: int = 3, p95_slow_ms: int = 60000) -> list[Proposal]:
    props: list[Proposal] = []
    field_fail: Counter = Counter()
    unresolved: Counter = Counter()
    gate: Counter = Counter()
    errors: Counter = Counter()
    timeouts: Counter = Counter()
    durations: dict[tuple[str, str], list[int]] = defaultdict(list)
    samples: dict[tuple, list[str]] = defaultdict(list)

    for e in events:
        plat = e.get("platform") or "generic"
        status = e.get("status", "")
        label = norm_label(e.get("field_label", ""))
        err = norm_text(e.get("error", "") or e.get("detail", "") or "")
        if e.get("duration_ms") and e.get("stage"):
            durations[(plat, e["stage"])].append(int(e["duration_ms"]))
        if e.get("gate_reason"):
            gate[(plat, norm_text(e["gate_reason"]))] += 1
        if e.get("event") == "unresolved_field" or status == "unresolved":
            if label:
                unresolved[label] += 1
            continue
        is_timeout = status == "timeout" or "timeout" in err or "timed out" in err
        if is_timeout and (e.get("stage") == "llm" or e.get("tier")):
            timeouts[e.get("tier") or "unknown"] += 1
            continue
        if status in ("error", "needs_human", "roadblock"):
            if label:
                field_fail[(plat, label)] += 1
                samples[("f", plat, label)].append(err)
            elif err:
                errors[(plat, err)] += 1

    for (plat, label), n in field_fail.items():
        if n >= min_count:
            props.append(Proposal(
                id=f"field-{plat}-{_slug(label)}", kind="code", tier=2, count=n, score=n * 3.0, platform=plat,
                title=f"{plat} field '{label}' fails {n}x: add handler",
                suggest=f"Fix the {plat} adapter/browser handler for the form field labelled '{label}' "
                        f"(fails {n} times; typical error: {(samples[('f', plat, label)] or [''])[0]}).",
                sample=samples[("f", plat, label)][:2]))
    for label, n in unresolved.items():
        if n >= min_count:
            props.append(Proposal(
                id=f"pattern-{_slug(label)}", kind="profile_pattern", tier=1, count=n, score=n * 2.0,
                title=f"no deterministic answer for '{label}' asked {n}x: add profile pattern",
                change={"op": "add_profile_pattern", "label": label,
                        "note": "maps the question to a profile key; the value stays in the user's profile"}))
    for tier, n in timeouts.items():
        if n >= min_count:
            props.append(Proposal(
                id=f"router-timeout-{_slug(tier)}", kind="config", tier=1, count=n, score=n * 2.5,
                title=f"router timeouts on '{tier}' tier {n}x: lower context / max_tokens",
                change={"op": "set_config", "key": f"llm.tiers.{tier}.max_tokens", "scale": 0.75}))
    for (plat, reason), n in gate.items():
        if n >= min_count * 2:
            props.append(Proposal(
                id=f"gate-{plat}-{_slug(reason)}", kind="config", tier=1, count=n, score=n * 0.5,
                platform=plat, title=f"gate '{reason}' blocks {n}x on {plat}: review threshold (advisory)"))
    for (plat, err), n in errors.items():
        if n >= min_count:
            props.append(Proposal(
                id=f"err-{plat}-{_slug(err)}", kind="code", tier=2, count=n, score=n * 2.0, platform=plat,
                title=f"{plat} error repeats {n}x: '{err[:60]}'",
                suggest=f"Investigate and fix repeated {plat} error: '{err}' ({n} occurrences)."))
    for (plat, stage), ds in durations.items():
        ds.sort()
        p95 = ds[min(len(ds) - 1, int(len(ds) * 0.95))]
        if len(ds) >= min_count and p95 >= p95_slow_ms:
            props.append(Proposal(
                id=f"slow-{plat}-{_slug(stage)}", kind="code", tier=2, count=len(ds), score=p95 / 10000,
                platform=plat, title=f"{plat} stage '{stage}' p95 {p95 // 1000}s over {len(ds)} runs",
                suggest=f"Profile and speed up the '{stage}' stage on {plat} (p95 {p95} ms)."))
    return sorted(props, key=lambda p: (-p.score, p.id))


def update_playbooks(props: list[Proposal], pb: PlaybookStore) -> int:
    """Tier 0: record mechanics-only lessons from clusters. Returns number of playbooks changed."""
    changed = 0
    for p in props:
        if p.kind == "code" and p.id.startswith("field-"):
            lab = p.id.split("-", 2)[2]
            changed += pb.add_lesson(p.platform, p.id, f"Field '{lab.replace('-', ' ')}' is fragile "
                                     f"({p.count} failures); verify the value landed before continuing.",
                                     tags=["field", "fragile"], evidence=p.count)
        elif p.kind == "code" and p.id.startswith("err-"):
            changed += pb.add_lesson(p.platform, p.id, f"Recurring error ({p.count}x): "
                                     f"{scrub(p.sample[0] if p.sample else p.title, 120)}",
                                     tags=["error"], evidence=p.count)
    return changed

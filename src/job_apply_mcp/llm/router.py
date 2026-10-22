"""Task-class + complexity router (ADR-0008).

1. classify: "review" / "narrative" / "field" (explicit or by cheap rules);
2. fields: score complexity -> tier (small/medium/large); narrative/review use their own configured tier;
3. pick the first available (provider, model) among primary + fallbacks, honoring allow_cloud;
4. call with per-provider semaphores, timeouts, retries with backoff;
5. validate JSON (+ optional validator): one repair attempt, then escalate one tier;
6. record token/latency stats per provider:model for health().
"""
from __future__ import annotations

import asyncio
import re
import time
from collections import defaultdict
from typing import Any

from ..config import LLMCfg, LLMTier, Settings
from ..core import registry
from . import providers as _providers  # noqa: F401  (registers built-ins)
from .base import (
    CallStats,
    ClientTaskRequired,
    Completion,
    LLMResult,
    ProviderError,
    Task,
    estimate_tokens,
    extract_json,
    validate_schema,
)


class LLMRouter:
    def __init__(self, settings: Settings | LLMCfg, concurrency: dict[str, int] | None = None,
                 providers: dict[str, Any] | None = None):
        if isinstance(settings, Settings):
            self.cfg, conc = settings.llm, settings.concurrency.llm_parallel
        else:
            self.cfg, conc = settings, concurrency or {"default": 4}
        self.parallel = conc
        self._providers: dict[str, Any] = dict(providers or {})
        self._sems: dict[str, asyncio.Semaphore] = {}
        self._heavy = asyncio.Semaphore(max(1, self.cfg.heavy_parallel))
        self._avail: dict[str, tuple[float, bool]] = {}
        self.stats: dict[str, CallStats] = defaultdict(CallStats)
        self.class_counts: dict[str, int] = defaultdict(int)
        self.escalation_count = 0

    # ---- providers -------------------------------------------------------------------------------
    def provider(self, name: str):
        if name not in self._providers:
            self._providers[name] = registry.llm_providers.create(name, timeout_s=self.cfg.timeout_s,
                                                                   **self.cfg.providers.get(name, {}))
        return self._providers[name]

    def _sem(self, name: str) -> asyncio.Semaphore:
        if name not in self._sems:
            self._sems[name] = asyncio.Semaphore(max(1, self.parallel.get(name, self.parallel.get("default", 4))))
        return self._sems[name]

    async def _available(self, name: str) -> bool:
        now = time.monotonic()
        hit = self._avail.get(name)
        if hit and now - hit[0] < self.cfg.availability_ttl_s:
            return hit[1]
        try:
            ok = await self.provider(name).available()
        except Exception:  # noqa: BLE001 - unknown provider or probe crash == unavailable
            ok = False
        self._avail[name] = (now, ok)
        return ok

    # ---- classification & scoring ----------------------------------------------------------------
    def classify(self, task: Task) -> str:
        if task.task_class:
            return task.task_class
        r = self.cfg.routing
        q = f"{task.question} {task.key}".lower()
        words = task.expected_chars / 6 if task.expected_chars else 0
        prose = (task.field_kind in (None, "textarea", "text")) and words >= r.narrative_min_words
        if prose or any(k in q for k in r.narrative_keywords):
            return "narrative"
        return "field"

    def score(self, task: Task) -> tuple[int, list[str]]:
        r, pts, why = self.cfg.routing, 0, []

        def add(n: int, reason: str):
            nonlocal pts
            if n:
                pts += n
                why.append(f"{reason}+{n}")

        add(r.kind_points.get(task.field_kind or "unknown", 1), f"kind:{task.field_kind or 'unknown'}")
        add(max((p for c, p in r.length_points if task.expected_chars >= c), default=0), "length")
        text = (task.question or task.prompt[:400]).lower()
        if any(k in text for k in r.essay_keywords):
            add(r.essay_points, "essay-kw")
        if task.company_specific:
            add(r.company_points, "company")
        add(r.failure_points * task.prior_failures, "prior-failure")
        return pts, why

    def tier_for_score(self, score: int) -> str:
        names, th = list(self.cfg.tiers), self.cfg.routing.thresholds
        best = names[0]
        for n in names:
            if score >= th.get(n, 0):
                best = n
        return best

    async def _self_check(self, task: Task) -> int | None:
        first = next(iter(self.cfg.tiers))
        q = (f"Rate from 0 to 10 how confident you are that you can answer this form question correctly "
             f"and concisely. Reply with a single integer.\nQuestion: {(task.question or task.prompt)[:300]}")
        try:
            res = await self._call_tier(first, self.cfg.tiers[first], q, Task(prompt=q, max_tokens=4), None)
        except Exception:  # noqa: BLE001
            return None
        m = re.search(r"\d+", res[0].text) if res else None
        return min(10, int(m.group())) if m else None

    async def decide(self, task: Task) -> dict[str, Any]:
        """Route decision without running anything: {task_class, tier, score, reasons}."""
        cls = self.classify(task)
        if cls in self.cfg.task_tiers:
            return {"task_class": cls, "tier": cls, "score": 0, "reasons": ["class"]}
        score, why = self.score(task)
        tier = self.tier_for_score(score)
        if self.cfg.routing.self_check and tier == next(iter(self.cfg.tiers)):
            conf = await self._self_check(task)
            if conf is not None and conf < self.cfg.routing.self_check_min:
                score += self.cfg.routing.confidence_points
                why.append(f"low-confidence:{conf}")
                tier = self.tier_for_score(score)
        return {"task_class": "field", "tier": tier, "score": score, "reasons": why}

    # ---- candidates ------------------------------------------------------------------------------
    def _tier_cfg(self, name: str) -> LLMTier:
        return self.cfg.tiers.get(name) or self.cfg.task_tiers[name]

    def _is_cloud(self, provider_name: str) -> bool:
        try:
            return bool(getattr(self.provider(provider_name), "is_cloud", True))
        except Exception:  # noqa: BLE001
            return True

    async def _candidates(self, tier: str) -> list[LLMTier]:
        out = []
        for c in [self._tier_cfg(tier), *self.cfg.fallbacks.get(tier, [])]:
            if not self.cfg.allow_cloud and self._is_cloud(c.provider):
                continue
            if not await self._available(c.provider):
                continue
            p = self.provider(c.provider)
            if hasattr(p, "has_model") and not await p.has_model(c.model):
                continue
            out.append(c)
        return out

    # ---- calling ---------------------------------------------------------------------------------
    async def _call_once(self, c: LLMTier, prompt: str, task: Task, schema: dict | None) -> Completion:
        p = self.provider(c.provider)
        kw: dict[str, Any] = {"model": c.model, "system": task.system, "json_schema": schema,
                                  "max_tokens": task.max_tokens or c.max_tokens,
                                  "temperature": c.temperature if c.temperature is not None else task.temperature}
        if c.provider == "client":
            kw["key"] = task.key
        fn = getattr(p, "complete_ex", None)
        if fn is None:
            text = await p.complete(prompt, **kw)
            return Completion(text, estimate_tokens(prompt), estimate_tokens(text))
        return await fn(prompt, **kw)

    async def _call_cand(self, c: LLMTier, prompt: str, task: Task, schema: dict | None) -> Completion:
        key = f"{c.provider}:{c.model}"
        st = self.stats[key]
        last: Exception | None = None
        for attempt in range(self.cfg.retries + 1):
            t0 = time.monotonic()
            try:
                async with self._sem(c.provider):
                    if c.heavy and c.provider != "client":
                        async with self._heavy:
                            comp = await asyncio.wait_for(self._call_once(c, prompt, task, schema),
                                                          self.cfg.timeout_s)
                    else:
                        comp = await asyncio.wait_for(self._call_once(c, prompt, task, schema), self.cfg.timeout_s)
            except ClientTaskRequired:
                raise
            except (ProviderError, TimeoutError) as e:
                st.errors += 1
                st.last_error = str(e)[:120] or type(e).__name__
                last = e
                if isinstance(e, ProviderError) and not e.retryable:
                    break
                if attempt < self.cfg.retries:
                    await asyncio.sleep(self.cfg.backoff_s * (2 ** attempt))
                continue
            st.calls += 1
            st.total_ms += (time.monotonic() - t0) * 1000
            st.tokens_in += comp.tokens_in
            st.tokens_out += comp.tokens_out
            return comp
        raise ProviderError(f"{key}: {last}") from last

    async def _call_tier(self, tier: str, _cfg: LLMTier, prompt: str, task: Task, schema: dict | None):
        """Try candidates in order. Returns (Completion, LLMTier) or raises ProviderError if none works."""
        errs = []
        for c in await self._candidates(tier):
            try:
                return await self._call_cand(c, prompt, task, schema), c
            except ProviderError as e:
                errs.append(str(e))
                self._avail.pop(c.provider, None)
        raise ProviderError("; ".join(errs) or f"no available provider for tier '{tier}'")

    @staticmethod
    def _validate(text: str, task: Task) -> tuple[Any, str | None]:
        data: Any = None
        if task.json_schema is not None:
            try:
                data = extract_json(text)
            except ValueError as e:
                return None, str(e)
            if task.json_schema and (err := validate_schema(data, task.json_schema)):
                return data, err
        if task.validator and (err := task.validator(text)):
            return data, err
        return data, None

    def _order(self, start: str) -> list[str]:
        """Escalation order. Task-class tiers are single-step; ordinary tiers go upward only."""
        names = list(self.cfg.tiers)
        if start in names:
            return names[names.index(start):]
        return [start, *names[-1:]]

    async def run(self, task: Task, *, force_tier: str | None = None) -> LLMResult:
        from .providers.client import current_sampler  # noqa: F401  (sampler is read inside the provider)

        d = await self.decide(task)
        if force_tier:
            d["tier"] = force_tier
        self.class_counts[d["task_class"]] += 1
        base = {"task_class": d["task_class"], "score": d["score"], "reasons": d["reasons"]}
        order = self._order(d["tier"])
        schema = task.json_schema
        esc, last_err = 0, ""
        for i, tier in enumerate(order):
            try:
                comp, cand = await self._call_tier(tier, self._tier_cfg(tier), task.prompt, task, schema)
            except ClientTaskRequired as e:
                ct = e.task
                return LLMResult(ok=True, client_task=ct, tier=tier, provider="client", model="client-default",
                                 escalations=esc, **base)
            except ProviderError as e:
                last_err = str(e)
                if i + 1 < len(order):
                    esc += 1
                    self.escalation_count += 1
                continue
            data, err = self._validate(comp.text, task)
            if err:
                repair = (f"{task.prompt}\n\nYour previous reply was invalid ({err}). Previous reply:\n"
                          f"{comp.text[:1500]}\nReturn a corrected reply only.")
                try:
                    comp2 = await self._call_cand(cand, repair, task, schema)
                    data, err = self._validate(comp2.text, task)
                    comp = comp2
                except (ProviderError, ClientTaskRequired) as e:
                    err = str(e)
            if not err:
                return LLMResult(ok=True, text=comp.text, data=data, tier=tier, provider=cand.provider,
                                 model=cand.model, escalations=esc, **base)
            last_err = f"{tier}: {err}"
            if i + 1 < len(order):
                esc += 1
                self.escalation_count += 1
        return LLMResult(ok=False, error=last_err or "no provider available", tier=order[-1],
                         escalations=esc, **base)

    async def review(self, draft: str, *, criteria: str = "", key: str = "") -> LLMResult:
        """Second-reader pass on a narrative using the configured `review` tier (default: calling agent)."""
        prompt = ("Review this draft for specificity, factual consistency with the candidate profile, "
                  "tone and length. Reply with a revised version, or the single word OK if it needs no change."
                  f"{chr(10) + 'Criteria: ' + criteria if criteria else ''}\n\nDraft:\n{draft}")
        return await self.run(Task(prompt=prompt, key=key, task_class="review", max_tokens=None))

    # ---- health ----------------------------------------------------------------------------------
    async def health(self) -> dict[str, Any]:
        names = sorted({c.provider for t in (*self.cfg.tiers.values(), *self.cfg.task_tiers.values(),
                                             *(f for fl in self.cfg.fallbacks.values() for f in fl))
                        for c in [t]})
        provs = {}
        for n in names:
            self._avail.pop(n, None)
            up = await self._available(n)
            row: dict[str, Any] = {"up": up, "cloud": self._is_cloud(n)}
            if up:
                try:
                    row["models"] = (await self.provider(n).list_models())[:20]
                except Exception:  # noqa: BLE001
                    row["models"] = []
            provs[n] = row
        return {
            "providers": provs,
            "tiers": {n: f"{t.provider}:{t.model}" for n, t in self.cfg.tiers.items()},
            "task_tiers": {n: f"{t.provider}:{t.model}" for n, t in self.cfg.task_tiers.items()},
            "allow_cloud": self.cfg.allow_cloud,
            "task_classes": dict(self.class_counts),
            "escalations": self.escalation_count,
            "stats": {k: v.view() for k, v in self.stats.items()},
        }



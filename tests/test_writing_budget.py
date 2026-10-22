"""Prose time budget, heavy-model semaphore timings and deferred drafting (ADR-0041)."""
import asyncio
import json
import time

from apply_fakes import POSTING, F, FakeAdapter, FakeGates, FakeProfile, FakeRouter, FakeStore, settings

from job_apply_mcp.apply.adapters import RouterView
from job_apply_mcp.apply.engine import ApplyEngine
from job_apply_mcp.config import LLMCfg, LLMTier
from job_apply_mcp.core.models import ApplicationForm, FieldKind
from job_apply_mcp.llm.base import LLM_TIMINGS
from job_apply_mcp.llm.providers.fake import FakeProvider
from job_apply_mcp.llm.router import LLMRouter

GOOD = ("I built a fleet scheduler in Python at Initech and maintained it for two years. It dispatches warehouse "
        "robots and removed the manual overrides that caused most order errors.")
TWO_PROSE = ApplicationForm(ref=POSTING.ref, fields=[
    F("first_name", "First Name"),
    F("q_why", "Why Acme Robotics?", FieldKind.textarea),
    F("q_how", "Describe a system you built end to end.", FieldKind.textarea),
])


def engine(router, form=None, **cfg):
    adapter = FakeAdapter(form=form) if form else FakeAdapter()
    return ApplyEngine(settings(**cfg), resolve_adapter=lambda ref: adapter, profile=FakeProfile(), router=router,
                       gates=FakeGates(), store=FakeStore())


class SlowRouter(FakeRouter):
    """Writer answers at once; the given task classes hang until cancelled."""

    def __init__(self, slow=("review",), delay=30.0, **kw):
        super().__init__(prose=GOOD, **kw)
        self.slow, self.delay, self.cancelled = slow, delay, 0

    async def complete(self, prompt, *, task_class="short", **kw):
        if task_class in self.slow:
            self.calls.append((task_class, prompt))
            try:
                await asyncio.sleep(self.delay)
            except asyncio.CancelledError:
                self.cancelled += 1
                raise
        return await super().complete(prompt, task_class=task_class, **kw)


async def test_budget_bounds_plan_and_cancels_llm_calls():
    r = SlowRouter(slow=("narrative",))
    e, t0 = engine(r, writing__timeout_s=0.3), time.monotonic()
    out = await e.plan(POSTING.ref)
    assert time.monotonic() - t0 < 2.0                           # not 30 s: the budget wins
    t = next(x for x in out.client_tasks if x["key"] == "q_why")
    assert t["timed_out"] and t["stage"] == "writer" and "draft" not in t   # nothing written yet
    assert r.cancelled == 1 and out.approval_id                  # the call was cancelled; planning finished


async def test_partial_draft_is_a_client_task_never_an_answer():
    r = SlowRouter(slow=("review",))
    e = engine(r, writing__timeout_s=0.3, writing__reviewer="router")
    out = await e.plan(POSTING.ref)
    t = next(x for x in out.client_tasks if x["key"] == "q_why")
    assert t["timed_out"] and t["draft"] == GOOD and t["stage"] == "review" and t["review"]
    assert "q_why" not in {a.key for a in out.plan.answers}
    assert "q_why" in out.plan.unresolved and "q_why" not in e._plans[POSTING.ref].answers
    res = await e.submit(POSTING.ref, dry_run=True)              # the approved plan cannot carry the draft
    assert res.status == "needs_human" and "q_why" in res.detail
    assert "q_why" not in json.dumps(out.compact().get("answers", {}))


def _router_settings(latency: float):
    def reply(prompt, model, n):
        return json.dumps({"pass": True, "violations": [], "corrected": ""}) if "Return JSON" in prompt else GOOD
    fake = FakeProvider("ollama", latency_s=latency, responses=reply)
    cfg = LLMCfg(tiers={"small": LLMTier(provider="ollama", model="m", timeout_s=5)},
                 task_tiers={"narrative": LLMTier(provider="ollama", model="m", timeout_s=7),
                             "review": LLMTier(provider="ollama", model="m")},
                 fallbacks={}, warmup=False, heavy_parallel=1)
    return LLMRouter(cfg, {"default": 4}, providers={"ollama": fake}), fake


async def test_heavy_semaphore_contention_shows_in_timings():
    llm, fake = _router_settings(0.15)
    e = engine(RouterView(llm), form=TWO_PROSE, writing__reviewer="router", writing__timeout_s=10)
    out = await e.plan(POSTING.ref)
    assert {a.key for a in out.plan.answers} >= {"q_why", "q_how"}
    assert fake.peak == 1                                        # narrative + review serialized router-wide
    assert out.timings["llm_gen"] >= 4 * 150 * 0.9               # 2 fields x (writer + review)
    assert out.timings["llm_wait"] >= 100                        # the second field queued behind the first
    assert llm.stats["ollama:m"].view()["avg_wait_ms"] > 0
    assert fake.calls[0]["timeout_s"] == 7                       # per-call timeout reaches the provider


async def test_router_records_wait_and_gen_into_sink():
    llm, _ = _router_settings(0.05)
    sink: dict[str, int] = {}
    tok = LLM_TIMINGS.set(sink)
    try:
        await asyncio.gather(*(RouterView(llm).complete("Write the answer", task_class="narrative") for _ in range(3)))
    finally:
        LLM_TIMINGS.reset(tok)
    assert sink["llm_gen"] >= 3 * 50 * 0.9 and sink["llm_wait"] >= 50  # 3 calls, 1 slot: 2 of them queued


async def test_deferred_mode_makes_no_prose_calls_and_draft_answers_fills_them():
    r = FakeRouter(prose=GOOD)
    e = engine(r, writing__mode="deferred", writing__reviewer="router")
    out = await e.plan(POSTING.ref)
    assert not [c for c, _ in r.calls if c in ("narrative", "review")]   # only the small-model batch may run
    t = next(x for x in out.client_tasks if x["key"] == "q_why")
    assert t["deferred"] and "draft" not in t
    drafted = await e.draft(POSTING.ref)
    assert {a.key: a.value for a in drafted.plan.answers}["q_why"] == GOOD
    assert "q_why" not in drafted.plan.unresolved and drafted.approval_id != out.approval_id
    assert {a.key for a in out.plan.answers} <= {a.key for a in drafted.plan.answers}

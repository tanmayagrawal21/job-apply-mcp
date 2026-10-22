import asyncio
from typing import ClassVar

import pytest
from apply_fakes import (
    FORM,
    POSTING,
    F,
    FakeAdapter,
    FakeDriver,
    FakeGates,
    FakeProfile,
    FakeRouter,
    FakeStore,
    settings,
)

from job_apply_mcp.apply.engine import ApplyEngine, canonical_digest
from job_apply_mcp.core.models import ApplicationForm, Decision, FieldKind

GOOD = "\n\n".join(["Acme Robotics builds software for warehouse fleets. I built a fleet scheduler in Python." * 2] * 4)


def engine(adapter=None, router=None, gates=None, store=None, drivers=None, control=None, **cfg):
    adapter = adapter or FakeAdapter()
    drivers = drivers or {}
    return ApplyEngine(settings(**cfg), resolve_adapter=lambda ref: adapter, profile=FakeProfile(), router=router,
                       gates=gates or FakeGates(), store=store or FakeStore(), control=control,
                       driver_factory=lambda n: drivers[n]), adapter


SHORT = {"q_source": {"value": "linkedin", "confidence": 0.9}, "q_tool": {"value": "bazel", "confidence": 0.9}}


async def test_plan_batches_short_fields_in_one_call_and_tasks_for_prose():
    r = FakeRouter(short=SHORT, prose="I am passionate.")  # lint fails -> prose becomes a task
    e, _ = engine(router=r, writing__max_rounds=1)
    out = await e.plan(POSTING.ref)
    ans = {a.key: a.value for a in out.plan.answers}
    assert ans["q_source"] == "Other" and ans["q_tool"] == "bazel"  # source from preference list; tool snapped
    assert ans["q_gender"] == "Decline To Self Identify" and ans["resume"] == "/tmp/resume.pdf"
    assert [c for c, _ in r.calls].count("short") == 1                           # ONE batched call
    assert out.plan.unresolved == ["q_why"] and out.client_tasks[0]["key"] == "q_why"
    assert "Acme Robotics" in out.client_tasks[0]["context"]["role"] and out.token
    assert set(out.timings) >= {"fetch", "gate", "map", "llm"}


async def test_low_confidence_invalid_and_sensitive_fields_never_answered_by_llm():
    form = ApplicationForm(ref=POSTING.ref, fields=[
        F("a", "How many years of Rust experience?"), F("b", "Pick one", FieldKind.select, options=["X", "Y"]),
        F("c", "Authorized to work in the US?", FieldKind.select, options=["Yes", "No"]),
        F("d", "Nickname")])
    r = FakeRouter(short={"b": {"value": "Z", "confidence": 0.99}, "d": {"value": "Ad", "confidence": 0.2},
                          "a": {"value": 5, "confidence": 1}, "c": {"value": "Yes", "confidence": 1}})
    e, _ = engine(adapter=FakeAdapter(form=form), router=r)
    out = await e.plan(POSTING.ref)
    assert out.plan.answers == []                       # invalid option, low confidence, and sensitive: all rejected
    assert set(out.plan.unresolved) == {"a", "b", "c", "d"}
    assert '"a"' not in r.calls[0][1] and '"c"' not in r.calls[0][1]


async def test_prose_pipeline_used_for_long_text_when_available():
    r = FakeRouter(short=SHORT, prose=GOOD)
    e, _ = engine(router=r)
    out = await e.plan(POSTING.ref)
    t = out.client_tasks[0]
    assert t["key"] == "q_why" and t["draft"] == GOOD and t["review"]            # client reviews a lint-clean draft
    r2 = FakeRouter(short=SHORT, prose=GOOD)
    e2, _ = engine(router=r2, writing__reviewer="router")
    out2 = await e2.plan(POSTING.ref)
    assert {a.key: a.value for a in out2.plan.answers}["q_why"] == GOOD and not out2.client_tasks


async def test_no_router_degrades_to_client_tasks():
    e, _ = engine(router=None)
    out = await e.plan(POSTING.ref)
    assert set(out.plan.unresolved) == {"q_tool", "q_why"}
    assert {t["key"] for t in out.client_tasks} == {"q_tool", "q_why"}


@pytest.mark.parametrize("decision,why", [(Decision.skip, "no sponsorship"), (Decision.needs_human, "attest")])
async def test_gates_stop_plan(decision, why):
    e, _ad = engine(gates=FakeGates(decision, [why]))
    out = await e.plan(POSTING.ref)
    assert out.plan.gate.decision == decision and not out.token and not out.plan.answers


async def test_duplicate_and_stop_phrase_fields():
    e, _ = engine(store=FakeStore(seen=True))
    out = await e.plan(POSTING.ref)
    assert out.plan.gate.decision == Decision.skip and "duplicate" in out.plan.gate.reasons
    form = ApplicationForm(ref=POSTING.ref, fields=[F("k", "I confirm this is my own work")])
    e2, _ = engine(adapter=FakeAdapter(form=form))
    assert (await e2.plan(POSTING.ref)).plan.gate.decision == Decision.needs_human


def _full_router():
    return FakeRouter(short=SHORT, prose="I am passionate.")


async def _planned(e):
    out = await e.plan(POSTING.ref)
    return out, {"q_why": "Because Acme Robotics builds warehouse fleets."}


async def test_token_required_for_real_submit_and_binds_answers():
    drv = FakeDriver()
    e, ad = engine(router=_full_router(), writing__max_rounds=1, drivers={"playwright": drv})
    out, extra = await _planned(e)
    r = await e.submit(POSTING.ref, extra, None, dry_run=False)
    assert r.status == "error" and "token" in r.detail and not ad.submits
    r = await e.submit(POSTING.ref, {**extra, "first_name": "Mallory"}, out.token, dry_run=False)
    assert r.status == "error" and "differs" in r.detail
    r = await e.submit(POSTING.ref, extra, "0" * 64, dry_run=False)
    assert r.status == "error"
    r = await e.submit(POSTING.ref, extra, out.token, dry_run=False)
    assert r.status == "submitted" and r.via == "api"
    sent, files, dry = ad.submits[0]
    assert sent["q_why"] == extra["q_why"] and sent["first_name"] == "Ada" and files == {"resume": "/tmp/resume.pdf"}
    assert "resume" not in sent and dry is False
    assert ad.fetches == 1                       # submit reused the cached plan context (no refetch)


async def test_stateless_full_answer_token_accepted_and_auto_approve():
    e, _ = engine(router=_full_router(), writing__max_rounds=1)
    out, extra = await _planned(e)
    full = {**{a.key: a.value for a in out.plan.answers}, **extra}
    e2, _ = engine(router=_full_router())          # fresh engine: no cached plan
    r = await e2.submit(POSTING.ref, full, canonical_digest(POSTING.ref, full), dry_run=False)
    assert r.status == "submitted"
    e3, ad3 = engine(router=_full_router(), apply__auto_approve=True, writing__max_rounds=1)
    r = await e3.submit(POSTING.ref, extra, None, dry_run=False)
    assert r.status == "submitted" and ad3.submits


async def test_dry_run_needs_no_token_and_is_not_recorded():
    store = FakeStore()
    e, ad = engine(router=_full_router(), store=store, writing__max_rounds=1)
    r = await e.submit(POSTING.ref, {"q_why": "text"}, None, dry_run=True)
    assert r.status == "dry_run" and ad.submits[0][2] is True and not store.records


async def test_missing_required_is_needs_human():
    e, _ = engine(router=_full_router(), apply__auto_approve=True, writing__max_rounds=1)
    r = await e.submit(POSTING.ref, {}, None, dry_run=False)
    assert r.status == "needs_human" and "q_why" in r.detail


async def test_falls_back_to_browser_and_unsupported_site_fallback():
    ad = FakeAdapter(submit_status="needs_browser")
    pw = FakeDriver(status="needs_human", detail="unsupported_site: nope")
    agent = FakeDriver(status="submitted")
    e, _ = engine(adapter=ad, router=_full_router(), apply__auto_approve=True, writing__max_rounds=1,
                  drivers={"playwright": pw, "browser_use": agent})
    r = await e.submit(POSTING.ref, {"q_why": "t"}, None, dry_run=False)
    assert r.status == "submitted" and r.via == "browser" and len(pw.calls) == 1 and len(agent.calls) == 1
    assert agent.calls[0][2] == {"resume": "/tmp/resume.pdf"}


async def test_no_driver_installed_is_needs_human():
    def boom(name):
        raise KeyError(name)
    ad = FakeAdapter(submit_status="needs_browser")
    e = ApplyEngine(settings(apply__auto_approve=True, writing__max_rounds=1), resolve_adapter=lambda r: ad,
                    profile=FakeProfile(), router=_full_router(), gates=FakeGates(), store=FakeStore(),
                    driver_factory=boom)
    r = await e.submit(POSTING.ref, {"q_why": "t"}, None, dry_run=False)
    assert r.status == "needs_human"


async def test_concurrent_same_company_submits_only_once():
    ad = FakeAdapter(delay=0.05)
    store = FakeStore()
    e, _ = engine(adapter=ad, router=_full_router(), store=store, apply__auto_approve=True, writing__max_rounds=1)
    rs = await asyncio.gather(*(e.submit(POSTING.ref, {"q_why": "t"}, None, dry_run=False) for _ in range(3)))
    assert sorted(r.status for r in rs) == ["duplicate", "duplicate", "submitted"] and len(ad.submits) == 1


@pytest.mark.parametrize("verdict,status", [("block", "needs_human"), ("needs_human", "needs_human")])
async def test_control_layer_can_block(verdict, status):
    class Ctl:
        calls: ClassVar[list] = []

        async def check(self, action, ctx):
            self.calls.append((action, ctx["ref"]))
            return {"decision": verdict, "reasons": ["policy"]}
    ctl = Ctl()
    e, ad = engine(router=_full_router(), control=ctl, apply__auto_approve=True, writing__max_rounds=1)
    r = await e.submit(POSTING.ref, {"q_why": "t"}, None, dry_run=False)
    assert r.status == status and not ad.submits and ctl.calls == [("submit", POSTING.ref)]


async def test_batch_pipeline_overlaps_planning_with_browser_and_gates_approval():
    class SlowAdapter(FakeAdapter):
        async def get_form(self, ref):
            await asyncio.sleep(0.05)
            return self.form.model_copy(update={"ref": ref})

        async def get_posting(self, ref, *, with_description=False):
            return POSTING.model_copy(update={"ref": ref, "title": f"Engineer {ref[-1]}", "company": f"Co{ref[-1]}"})

    ad = SlowAdapter(delay=0.15)
    e, _ = engine(adapter=ad, router=_full_router(), apply__auto_approve=True, writing__max_rounds=1,
                  concurrency__browser_workers=1)
    form = FORM.model_copy(update={"fields": [f for f in FORM.fields if f.key != "q_why"]})
    ad.form = form
    refs = [f"greenhouse:b:{i}" for i in range(4)]
    import time
    t0 = time.monotonic()
    rows = await e.apply_batch(refs, dry_run=False)
    took = time.monotonic() - t0
    assert [r["status"] for r in rows] == ["submitted"] * 4 and all("t" in r for r in rows)
    assert took < 4 * (0.05 + 0.15)             # serial would be 0.8s; planning overlaps submission
    e2, _ = engine(adapter=ad, router=_full_router(), writing__max_rounds=1)
    rows = await e2.apply_batch(refs[:1], dry_run=False)
    assert rows[0]["status"] == "awaiting_approval" and rows[0]["token"]
    rows = await e2.apply_batch(refs[:1], dry_run=True)
    assert rows[0]["status"] == "dry_run"

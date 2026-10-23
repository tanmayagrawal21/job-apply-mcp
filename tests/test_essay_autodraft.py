"""ADR-0057: required essays drafted locally, strictly reviewed locally, filled only on a pass and labelled assumed.
Offline: fake routers/providers, fictional postings and facts."""
from __future__ import annotations

import asyncio
import json
from pathlib import Path

import pytest
from apply_fakes import POSTING, F, FakeAdapter, FakeGates, FakeProfile, FakeStore, settings
from test_approvals import _cfg
from test_intake import MultiAdapter, make

from job_apply_mcp.apply import essays
from job_apply_mcp.apply.adapters import RouterView
from job_apply_mcp.apply.engine import ApplyEngine
from job_apply_mcp.config import LLMCfg, LLMTier
from job_apply_mcp.core.models import ApplicationForm, FieldKind
from job_apply_mcp.llm.base import Task
from job_apply_mcp.llm.providers.fake import FakeProvider
from job_apply_mcp.llm.router import LLMRouter

GOOD = ("I built a fleet scheduler in Python at Initech and maintained it for two years. It dispatches warehouse "
        "robots and removed the manual overrides that caused most order errors.")
PASS = {"pass": True, "violations": [], "corrected": ""}
FAIL = {"pass": False, "violations": ["claim not supported by the facts"], "corrected": ""}
FORM = ApplicationForm(ref=POSTING.ref, fields=[
    F("first_name", "First Name"),
    F("q_why", "Why do you want to work at Acme Robotics?", FieldKind.textarea),
])


class EssayRouter:
    """Answers each task class from a table; records every call."""

    def __init__(self, prose=GOOD, review=PASS, avail=True, hang=()):
        self.calls: list[tuple[str, str]] = []
        self.prose, self.review, self.avail, self.hang = prose, review, avail, set(hang)

    async def available(self, task_class):
        return self.avail if task_class.startswith("essay") else True

    async def complete(self, prompt, *, task_class="short", system=None, max_tokens=256, json_schema=None):
        self.calls.append((task_class, prompt))
        if task_class in self.hang:
            await asyncio.sleep(30)
        if task_class == "short":
            return "{}"
        if task_class in ("review", "essay_review"):
            return json.dumps(self.review)
        return self.prose


def engine(tmp_path, router, *, form=FORM, trusted=True, reviewer="router", autonomy="infer", **cfg):
    s = settings(writing__reviewer=reviewer, writing__timeout_s=5, **cfg)
    s.answers.autonomy = autonomy
    s.paths.data_dir = str(tmp_path)
    s._autodraft_trusted = trusted
    adapter = FakeAdapter(form=form)
    return ApplyEngine(s, resolve_adapter=lambda ref: adapter, profile=FakeProfile(), router=router,
                       gates=FakeGates(), store=FakeStore())


def classes(r):
    return [c for c, _ in r.calls if c != "short"]


# ------------------------------------------------------------------------------------------------ engine
@pytest.mark.parametrize("autonomy", ["safe_default", "infer"])
async def test_required_essay_drafted_reviewed_filled_and_labelled(tmp_path, autonomy):
    r = EssayRouter()
    e = engine(tmp_path, r, autonomy=autonomy)
    out = await e.plan(POSTING.ref)
    a = {x.key: x for x in out.plan.answers}["q_why"]
    assert a.value == GOOD and a.assumed and a.source == essays.RULE and "full text:" in a.rationale
    assert classes(r) == ["essay", "essay_review"]                     # local writer + local strict reviewer only
    assert "q_why" not in out.plan.unresolved
    d = out.compact()
    assert d["assumed"]["q_why"]["rule"] == essays.RULE and d["assumed"]["q_why"]["value"].endswith("…")
    assert out.plan.audit["assumed"]["q_why"]["rule"] == essays.RULE
    files = list(essays.essay_dir(tmp_path, POSTING.ref).glob("*.md"))
    assert len(files) == 1 and GOOD in files[0].read_text() and "filled" in files[0].read_text()
    ev = json.loads((files[0].parent.parent / "assumed.json").read_text())
    assert {row["key"]: row["rule"] for row in ev["assumed"]}["q_why"] == essays.RULE


async def test_review_failure_leaves_required_essay_unresolved_never_filled(tmp_path):
    r = EssayRouter(review=FAIL)
    e = engine(tmp_path, r, writing__max_rounds=2)
    out = await e.plan(POSTING.ref)
    assert "q_why" not in {a.key for a in out.plan.answers} and "q_why" in out.plan.unresolved
    t = next(x for x in out.client_tasks if x["key"] == "q_why")
    assert t["needs_human"] and t["autodraft"] == "rejected by review" and "draft" not in t
    assert GOOD not in json.dumps(out.compact())                       # weak text never leaves the local file
    f = next(essays.essay_dir(tmp_path, POSTING.ref).glob("*.md")).read_text()
    assert "NOT filled" in f and "claim not supported" in f and GOOD in f
    res = await e.submit(POSTING.ref, dry_run=True)
    assert res.status == "needs_human"


async def test_timeout_leaves_required_essay_unresolved(tmp_path):
    r = EssayRouter(hang=("essay_review",))
    e = engine(tmp_path, r)
    e.s.writing.timeout_s = 0.2
    out = await e.plan(POSTING.ref)
    t = next(x for x in out.client_tasks if x["key"] == "q_why")
    assert "timed out" in t["autodraft"] and t["needs_human"] and "q_why" in out.plan.unresolved
    assert "q_why" not in {a.key for a in out.plan.answers}


async def test_fabricated_number_or_name_is_never_filled_even_if_the_reviewer_passes(tmp_path):
    r = EssayRouter(prose=GOOD + " It cut order errors by 47% at Globex.")
    e = engine(tmp_path, r, writing__max_rounds=1)
    out = await e.plan(POSTING.ref)
    t = next(x for x in out.client_tasks if x["key"] == "q_why")
    assert any("grounding" in v for v in t["violations"]) and "essay_review" not in classes(r)
    assert "q_why" in out.plan.unresolved


async def test_no_local_models_means_unresolved_never_cloud(tmp_path):
    r = EssayRouter(avail=False)
    e = engine(tmp_path, r)
    out = await e.plan(POSTING.ref)
    assert classes(r) == [] and "q_why" in out.plan.unresolved
    assert next(x for x in out.client_tasks if x["key"] == "q_why")["autodraft"] == "local_models_unavailable"


OPTIONAL = ApplicationForm(ref=POSTING.ref, fields=[
    F("first_name", "First Name"),
    F("q_extra", "Tell us about a project you are proud of.", FieldKind.textarea, required=False),
])


async def test_optional_essay_blank_under_safe_default(tmp_path):
    r = EssayRouter()
    out = await engine(tmp_path, r, form=OPTIONAL, autonomy="safe_default", apply__write_optional=True).plan(POSTING.ref)
    assert classes(r) == [] and "q_extra" not in {a.key for a in out.plan.answers}
    assert "q_extra" not in out.plan.unresolved


async def test_optional_essay_drafted_under_infer_and_dropped_on_failure(tmp_path):
    ok = await engine(tmp_path, EssayRouter(), form=OPTIONAL).plan(POSTING.ref)
    assert {a.key: a.source for a in ok.plan.answers}["q_extra"] == essays.RULE
    bad = await engine(tmp_path / "b", EssayRouter(review=FAIL), form=OPTIONAL, writing__max_rounds=1).plan(POSTING.ref)
    assert "q_extra" not in {a.key for a in bad.plan.answers} and "q_extra" not in bad.plan.unresolved


@pytest.mark.parametrize(("reviewer", "autonomy", "trusted"), [
    ("client", "infer", True), ("router", "strict", True), ("router", "infer", False)])
async def test_unchanged_without_trusted_router_reviewer_and_autonomy(tmp_path, reviewer, autonomy, trusted):
    r = EssayRouter()
    e = engine(tmp_path, r, reviewer=reviewer, autonomy=autonomy, trusted=trusted)
    assert not e.s.essay_autodraft
    out = await e.plan(POSTING.ref)
    assert not [c for c in classes(r) if c.startswith("essay")]          # the old narrative path only
    assert not [a for a in out.plan.answers if a.source == essays.RULE]
    assert not essays.essay_dir(tmp_path, POSTING.ref).exists()


async def test_deferred_plan_then_draft_answers_labels_and_merges_evidence(tmp_path):
    r = EssayRouter()
    e = engine(tmp_path, r, writing__mode="deferred")
    out = await e.plan(POSTING.ref)
    assert "q_why" in out.plan.unresolved and classes(r) == []
    drafted = await e.draft(POSTING.ref)
    assert {a.key: a.source for a in drafted.plan.answers}["q_why"] == essays.RULE
    assert drafted.plan.unresolved == [] and drafted.approval_id
    ev = Path(tmp_path) / "evidence"
    assert any("q_why" in p.read_text() for p in ev.glob("*/assumed.json"))


# ------------------------------------------------------------------------------------------------ router
def _router(task_tiers, fallbacks=None, allow_cloud=True):
    fake = FakeProvider("ollama", responses=lambda p, m, n: "ok")
    cfg = LLMCfg(tiers={"small": LLMTier(provider="ollama", model="m")}, task_tiers=task_tiers,
                 fallbacks=fallbacks or {}, warmup=False, allow_cloud=allow_cloud)
    return LLMRouter(cfg, {"default": 2}, providers={"ollama": fake}), fake


async def test_local_only_class_skips_client_and_never_hands_off():
    llm, _ = _router({"essay": LLMTier(provider="client", model="client-default")})
    res = await llm.run(Task(prompt="write", task_class="essay"))
    assert not res.ok and res.client_task is None                      # no cloud, no client hand-off
    assert not await RouterView(llm).available("essay")
    llm2, fake = _router({"essay": LLMTier(provider="client", model="client-default")},
                         {"essay": [LLMTier(provider="ollama", model="m")]})
    res = await llm2.run(Task(prompt="write", task_class="essay"))
    assert res.ok and res.provider == "ollama" and fake.calls


async def test_local_only_class_without_a_tier_fails_closed():
    llm, fake = _router({})
    res = await llm.run(Task(prompt="write", task_class="essay_review"))
    assert not res.ok and not fake.calls


# ------------------------------------------------------------------------------------------------ trust
USER = '[writing]\nreviewer = "router"\n[answers]\nautonomy = "infer"\n'


def test_trusted_user_config_enables_autodraft(tmp_path, monkeypatch):
    assert _cfg(tmp_path, monkeypatch, user=USER).essay_autodraft


@pytest.mark.parametrize("kw", [
    {"proj": '[writing]\nreviewer = "router"\n', "user": '[answers]\nautonomy = "infer"\n'},
    {"env": {"JOB_APPLY__WRITING__REVIEWER": "router"}, "user": '[answers]\nautonomy = "infer"\n'},
    {"user": USER, "env": {"JOB_APPLY__LLM__TASK_TIERS__ESSAY_REVIEW__MODEL": "other:1b"}},
    {"user": USER, "env": {"JOB_APPLY__LLM__ALLOW_CLOUD": "false"}},
    {"user": USER, "proj": '[llm]\nlocal_only_classes = []\n'},
    {"user": USER, "proj": '[writing]\nbanned_phrases = []\n'},
    {"user": USER, "via_env": True},
    {"user": USER, "mode": 0o666},
])
def test_autodraft_never_enabled_or_widened_outside_the_trusted_user_config(tmp_path, monkeypatch, kw):
    assert not _cfg(tmp_path, monkeypatch, **kw).essay_autodraft


def test_env_can_still_switch_autodraft_off(tmp_path, monkeypatch):
    s = _cfg(tmp_path, monkeypatch, user=USER, env={"JOB_APPLY__ANSWERS__AUTONOMY": "strict"})
    assert s.answers.autonomy == "strict" and not s.essay_autodraft


def test_one_task_tier_is_configurable_without_dropping_the_others(tmp_path, monkeypatch):
    s = _cfg(tmp_path, monkeypatch, user=USER + '[llm.task_tiers.essay]\nmodel = "fict-writer:12b"\n')
    t = s.llm.task_tiers
    assert t["essay"].model == "fict-writer:12b" and t["essay"].provider == "ollama"
    assert {"narrative", "review", "essay_review"} <= set(t) and s.essay_autodraft


# ------------------------------------------------------------------------------------------------ run path
async def test_run_applications_autodrafts_required_essay_and_reports_it(tmp_path):
    prose = "https://boards.greenhouse.io/wordy/jobs/1"
    r, _ad, eng = make(tmp_path, adapter=MultiAdapter(prose_for=[prose]), router=EssayRouter(),
                       writing__reviewer="router", writing__timeout_s=5)
    eng.s.answers.autonomy = "safe_default"
    eng.s._autodraft_trusted = True
    rep = await r.run(links=[prose, "https://boards.greenhouse.io/plain/jobs/2"], sources=["links"])
    assert rep["counts"] == {"dry_run_ok": 2}, rep
    assumed = rep["plan_context"].get("assumed") or rep.get("assumed")
    row = next(x for x in assumed[prose] if x["key"] == "q_why")
    assert row["rule"] == essays.RULE and len(row["value"]) <= 120


async def test_run_applications_rejected_essay_needs_the_owner(tmp_path):
    prose = "https://boards.greenhouse.io/wordy/jobs/1"
    r, ad, eng = make(tmp_path, adapter=MultiAdapter(prose_for=[prose]), router=EssayRouter(review=FAIL),
                      writing__reviewer="router", writing__timeout_s=5, writing__max_rounds=1)
    eng.s.answers.autonomy = "infer"
    eng.s._autodraft_trusted = True
    rep = await r.run(links=[prose], sources=["links"])
    assert rep["counts"] == {"needs_human": 1} and ad.submits == []


async def test_claiming_to_work_at_the_target_company_is_never_filled(tmp_path):
    r = EssayRouter(prose="As a fleet engineer at Acme Robotics, I built a fleet scheduler in Python at Initech.")
    out = await engine(tmp_path, r, writing__max_rounds=1).plan(POSTING.ref)
    t = next(x for x in out.client_tasks if x["key"] == "q_why")
    assert any("claims employment at Acme Robotics" in v for v in t["violations"])
    assert "q_why" in out.plan.unresolved


async def test_essay_reviewer_is_verdict_only_and_fenced(tmp_path):
    r = EssayRouter()
    await engine(tmp_path, r).plan(POSTING.ref)
    prompt = next(p for c, p in r.calls if c == "essay_review")
    assert "<<<DRAFT-" in prompt and '"corrected"' not in prompt and "FACTS:" in prompt


def test_long_essay_is_not_judged_by_cover_letter_rules():
    from job_apply_mcp.apply.writing import lint
    from job_apply_mcp.config import WritingCfg
    text = " ".join(f"Sentence {w} about the scheduler and its robots." for w in "abcdefghijklmnopqrstu")   # > 600 chars
    kinds = {v.kind for v in lint(text, WritingCfg(), company="Acme Robotics", essay=True)}
    assert len(text) > 600 and not kinds & {"structure", "company", "addressee", "closing"}
    assert {"structure", "company"} <= {v.kind for v in lint(text, WritingCfg(), company="Acme Robotics")}

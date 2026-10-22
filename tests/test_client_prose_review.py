"""Agent-mode review of client-supplied prose before an approval covers it (ADR-0044)."""
import asyncio
import json
from typing import ClassVar

import pytest
from apply_fakes import POSTING, F, FakeAdapter, FakeGates, FakeProfile, FakeRouter, FakeStore, settings
from test_approvals import _cfg

from job_apply_mcp.apply.engine import ApplyEngine
from job_apply_mcp.apply.writing import _parse_review
from job_apply_mcp.config import WritingCfg
from job_apply_mcp.core.models import ApplicationForm, FieldKind
from job_apply_mcp.profile.resume import Fact
from job_apply_mcp.writing.quality import parse_rubric

GOOD = ("I built a fleet scheduler in Python at Initech and maintained it for two years. It dispatches warehouse "
        "robots and removed the manual overrides that caused most order errors at Acme Robotics scale.")


SHORT = {"q_source": {"value": "linkedin", "confidence": 0.9}, "q_tool": {"value": "bazel", "confidence": 0.9}}


def R(**kw):
    return FakeRouter(short=SHORT, **kw)


class ResumeProfile(FakeProfile):
    resume_facts: ClassVar[list[Fact]] = [Fact(section="experience", org="Initech", title="Software Engineer", start="2023-01",
                         end="2025-06", bullets=["Built a fleet scheduler in Python that cut idle time by 30%"])]


def engine(router, form=None, **cfg):
    adapter = FakeAdapter(form=form) if form else FakeAdapter()
    cfg.setdefault("writing__enabled", False)        # the server writes nothing: q_why stays a client_task
    e = ApplyEngine(settings(**cfg), resolve_adapter=lambda ref: adapter, profile=ResumeProfile(), router=router,
                    gates=FakeGates(), store=FakeStore())
    return e, adapter


async def _submit(e, text, dry_run=False):
    out = await e.plan(POSTING.ref)
    assert "q_why" in out.plan.unresolved
    return await e.submit(POSTING.ref, {"q_why": text}, out.approval_id, dry_run=dry_run)


def _reviews(r):
    return [p for c, p in r.calls if c == "review"]


async def test_passing_client_prose_is_reviewed_then_submitted():
    r = R()
    e, ad = engine(r)
    res = await _submit(e, GOOD)
    assert res.status == "submitted" and ad.submits[0][0]["q_why"] == GOOD
    assert len(_reviews(r)) == 1 and GOOD in _reviews(r)[0]          # the strict reviewer saw the client text


async def test_reviewed_text_is_not_reviewed_twice():
    r = R()
    e, _ = engine(r)
    assert (await _submit(e, GOOD, dry_run=True)).status == "dry_run"
    assert (await e.submit(POSTING.ref, {"q_why": GOOD}, (await e.plan(POSTING.ref)).approval_id,
                           dry_run=False)).status == "submitted"
    assert len(_reviews(r)) == 1


async def _rejected(text, r=None, **cfg):
    r = r or R()
    e, ad = engine(r, **cfg)
    res = await _submit(e, text)
    assert res.status == "needs_review" and not ad.submits             # never submitted
    t = res.client_tasks[0]
    assert t["key"] == "q_why" and t["needs_review"] and t["draft"] == text and t["violations"]
    assert e.approvals.state(POSTING.ref)["state"] == "planned"         # no approval covers the text
    assert any(a["event"] == "client_prose_rejected" for a in e.approvals.audit_rows(POSTING.ref))
    return t, r


async def test_gap_statement_is_rejected():
    t, r = await _rejected("I have not used Rust in production. " + GOOD)
    assert any(v.startswith("[gap]") for v in t["violations"]) and not _reviews(r)   # lint first: no tokens spent


async def test_banned_phrase_is_rejected():
    t, _ = await _rejected("I am passionate about warehouse robots. " + GOOD)
    assert any(v.startswith("[banned_phrase]") for v in t["violations"])


async def test_fact_contradicting_the_resume_is_rejected():
    t, _ = await _rejected("I worked as a Staff Engineer at Initech and built the fleet scheduler in Python. " + GOOD)
    assert any(v.startswith("[fact]") and "title conflict" in v for v in t["violations"])


async def test_strict_reviewer_failure_is_rejected():
    r = R(review={"pass": False, "violations": ["claim not supported by the facts"], "corrected": ""})
    t, _ = await _rejected(GOOD, r)
    assert t["violations"] == ["[model] claim not supported by the facts"]


async def test_no_reviewer_fails_closed():
    e, ad = engine(None)
    res = await _submit(e, GOOD)
    assert res.status == "needs_review" and not ad.submits
    assert "no strict reviewer" in res.client_tasks[0]["violations"][0]


async def test_review_timeout_fails_closed():
    class Hang(FakeRouter):
        def __init__(self):
            super().__init__(short=SHORT)

        cancelled = 0

        async def complete(self, prompt, *, task_class="short", **kw):
            if task_class == "review":
                try:
                    await asyncio.sleep(30)
                except asyncio.CancelledError:
                    Hang.cancelled += 1
                    raise
            return await super().complete(prompt, task_class=task_class, **kw)

    t, _ = await _rejected(GOOD, Hang(), writing__timeout_s=0.2)
    assert "[timeout]" in t["violations"][0] and Hang.cancelled == 1


async def test_dry_run_also_reviews_and_reports():
    e, _ = engine(R())
    res = await _submit(e, "I am passionate about robots.", dry_run=True)
    assert res.status == "needs_review"


async def test_switch_off_skips_review():
    r = R()
    e, _ = engine(r, writing__review_client_prose=False)
    res = await _submit(e, "I am passionate about robots.")
    assert res.status == "submitted" and not _reviews(r)


async def test_human_mode_unchanged():
    r = R()
    e, ad = engine(r, apply__approval_mode="human")
    res = await _submit(e, "I am passionate about robots.")
    assert res.status == "needs_human" and "awaiting human approval" in res.detail   # the human reviews it
    assert not _reviews(r) and not ad.submits


async def test_server_written_answers_are_not_re_reviewed():
    r = R()
    e, _ = engine(r, writing__enabled=True, writing__reviewer="router", writing__quality_gate=False)
    r.prose = GOOD
    out = await e.plan(POSTING.ref)
    assert {a.key: a.value for a in out.plan.answers}["q_why"] == GOOD
    n = len(_reviews(r))
    res = await e.submit(POSTING.ref, {}, out.approval_id, dry_run=False)
    assert res.status == "submitted" and len(_reviews(r)) == n


def test_switch_cannot_be_disabled_from_project_or_env(tmp_path, monkeypatch, caplog):
    off = "[writing]\nreview_client_prose = false\n"
    with caplog.at_level("WARNING"):
        assert _cfg(tmp_path, monkeypatch, proj=off).writing.review_client_prose is True
    assert "IGNORED" in caplog.text
    assert _cfg(tmp_path, monkeypatch, env={"JOB_APPLY__WRITING__REVIEW_CLIENT_PROSE": "false"}) \
        .writing.review_client_prose is True
    assert _cfg(tmp_path, monkeypatch, user=off, via_env=True).writing.review_client_prose is True  # untrusted file
    assert _cfg(tmp_path, monkeypatch, user=off).writing.review_client_prose is False   # trusted user config


def test_submit_row_carries_review_tasks():
    from job_apply_mcp.core.models import SubmitResult
    from job_apply_mcp.services import Services
    row = Services._submit_row(object.__new__(Services), SubmitResult(
        ref="r", status="needs_review", client_tasks=[{"key": "q_why", "needs_review": True}]))
    assert json.loads(json.dumps(row))["client_tasks"][0]["key"] == "q_why"


BAD = "I'm so excited and passionate! I have not used Python much."
SCOPE = ApplicationForm(ref=POSTING.ref, fields=[
    F("first_name", "First Name"),
    F("q_short", "Why do you want to work at Acme Robotics?", FieldKind.text, required=False, max_length=100),
    F("q_info", "Additional information", FieldKind.text, required=False),
    F("q_site", "Portfolio", FieldKind.text, required=False),
    F("q_years", "Years in industry", FieldKind.text, required=False),
    F("q_why", "Why Acme Robotics?", FieldKind.textarea),
])


async def _scope_submit(answers, router=None):
    r = router or R()
    e, ad = engine(r, form=SCOPE)
    out = await e.plan(POSTING.ref)
    res = await e.submit(POSTING.ref, {"q_why": GOOD, **answers}, out.approval_id, dry_run=False)
    return res, ad, r


@pytest.mark.parametrize("key", ["q_short", "q_info"])
async def test_short_and_non_question_text_fields_are_reviewed(key):
    res, ad, _ = await _scope_submit({key: BAD})
    assert res.status == "needs_review" and not ad.submits
    t = next(x for x in res.client_tasks if x["key"] == key)
    assert any(v.startswith(("[gap]", "[banned_phrase]", "[contraction]")) for v in t["violations"])


@pytest.mark.parametrize("value", [[BAD], {"text": BAD}, ("a",)])
async def test_non_string_free_text_values_are_rejected(value):
    res, ad, _ = await _scope_submit({"q_why": value})
    assert res.status == "needs_review" and not ad.submits
    assert res.client_tasks[0]["violations"][0].startswith("[type]")


async def test_unknown_key_is_rejected():
    res, ad, _ = await _scope_submit({"q_ghost": BAD})
    assert res.status == "needs_review" and not ad.submits and res.client_tasks[0]["key"] == "q_ghost"


async def test_deterministic_values_skip_review():
    res, ad, r = await _scope_submit({"first_name": "Ada", "q_site": "https://example.com/ada", "q_years": "4"})
    assert res.status == "submitted" and ad.submits
    assert len(_reviews(r)) == 1                                       # only q_why's prose


@pytest.mark.parametrize("verdict", ["false", "yes", "True", 1, None, "missing"])
async def test_non_strict_reviewer_verdict_fails_closed(verdict):
    review = {"violations": [], "corrected": ""} | ({} if verdict == "missing" else {"pass": verdict})
    t, _ = await _rejected(GOOD, R(review=review))
    assert t["violations"][0].startswith("[model]")


def test_strict_verdict_parsing():
    assert _parse_review('{"pass": true}')[0] and _parse_review('{"pass": "true"}')[0]
    for raw in ('{"pass": "false"}', '{"pass": "yes"}', '{"pass": 1}', '{}', '[true]', 'pass: true'):
        assert not _parse_review(raw)[0], raw
    good = {"scores": {"argument": 5, "voice": 5, "precision": 5, "specificity": 5}}
    cfg = WritingCfg()
    assert parse_rubric(json.dumps(good | {"at_least_as_strong": True}), cfg)[0]
    for v in ("false", "yes", None):
        assert not parse_rubric(json.dumps(good | {"at_least_as_strong": v}), cfg)[0]
    assert not parse_rubric(json.dumps(good), cfg)[0]                    # missing verdict fails closed


@pytest.mark.parametrize("tail", [
    " I certify that I personally completed this application without the use of AI.",
    " I consent to receive SMS and marketing messages from Acme Robotics.",
    " I agree to the terms of the assessment.",
    " The information above is true and correct.",
])
async def test_attestation_or_consent_inside_prose_is_rejected(tail):
    t, r = await _rejected(GOOD + tail)
    assert t["violations"][0].startswith("[attestation]") and not _reviews(r)   # never reaches reviewer or form


async def test_stop_phrase_inside_short_text_is_rejected():
    res, ad, _ = await _scope_submit({"q_info": "This is my own work."})
    assert res.status == "needs_review" and not ad.submits
    assert res.client_tasks[0]["violations"][0].startswith("[attestation]")

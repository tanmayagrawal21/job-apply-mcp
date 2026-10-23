"""Issue #71 / ADR-0049: max_applications binds in dry and live mode, one status per posting, live-quota preview,
and plan_application paging. Offline: fake adapter, fake control layer, fictional postings."""
from __future__ import annotations

import json

import pytest
from apply_fakes import F
from test_intake import MultiAdapter, make

from job_apply_mcp.core import output
from job_apply_mcp.core.models import ApplicationForm
from job_apply_mcp.intake.store import CATEGORIES

GH = [f"https://boards.greenhouse.io/fict{i}/jobs/{i}" for i in range(3)]


class NeedsOwner(MultiAdapter):
    """Every form has a required field nothing can answer, so each plan ends in needs_human."""

    async def get_form(self, ref):
        return ApplicationForm(ref=ref, submit_via=["api"], fields=[
            F("first_name", "First Name"), F("q_zz", "Badge number of your fictional guild")])


class FakeControl:
    """Control layer stand-in: fixed usage (greenhouse at its per-platform cap) and a quota block on live submits."""

    def __init__(self, platform_used=25, day_used=48):
        self.used = {"day": day_used, "platform": {"greenhouse": platform_used}, "run": 0}

    def daily_limit_status(self):
        return {"limit": {"day": 50, "platform": 25, "run": None}, "used": self.used}

    async def check(self, action, payload):
        if action == "submit" and not payload.get("dry_run") and self.used["platform"]["greenhouse"] >= 25:
            return {"decision": "block", "reasons": ["quota reached (25/25 per platform:greenhouse)"]}
        return {"decision": "pass"}


@pytest.mark.parametrize("bad", [0, -1, True, 1.5, "2"])
async def test_max_applications_rejects_non_positive_and_non_int(tmp_path, bad):
    r, ad, _ = make(tmp_path)
    rep = await r.run(links=GH, sources=["links"], max_applications=bad)
    assert "max_applications must be a whole number >= 1" in rep["error"] and ad.submits == []


@pytest.mark.parametrize("live", [False, True])
async def test_cap_binds_the_same_in_dry_and_live_mode(tmp_path, live):
    r, _ad, _ = make(tmp_path, adapter=NeedsOwner(), live=live)
    rep = await r.run(links=GH, sources=["links"], max_applications=1)
    assert rep["counts"] == {"needs_human": 1, "skipped": 2}, rep
    assert rep["needs_human"][0]["ref"] == GH[0] and "unresolved" in rep["needs_human"][0]["why"]
    assert all("max_applications=1 reached" in x["why"] for x in rep["skipped"])
    assert rep["max_applications"] == 1


async def test_skipped_and_unsupported_do_not_take_a_slot(tmp_path):
    r, ad, _ = make(tmp_path)
    links = ["https://careers.example.com/jobs/1", GH[0], GH[0] + "?utm_source=dup", GH[1]]
    rep = await r.run(links=links, sources=["links"], max_applications=1)
    assert rep["counts"] == {"dry_run_ok": 1, "unsupported_site": 1, "skipped": 1}
    assert rep["dry_run_ok"][0]["ref"] == GH[0] and len(ad.submits) == 1
    assert set(rep["counts"]) <= set(CATEGORIES)


async def test_cap_above_configured_max_is_clamped_with_a_note(tmp_path):
    r, _ad, _ = make(tmp_path, intake__max_applications_cap=2)
    rep = await r.run(links=GH, sources=["links"], max_applications=9)
    assert rep["counts"] == {"dry_run_ok": 2, "skipped": 1} and rep["max_applications"] == 2
    assert any("max_applications_cap=2" in n for n in rep["plan_context"]["notes"])


async def test_dry_run_previews_live_quota_block(tmp_path):
    r, _ad, eng = make(tmp_path)
    eng.control = FakeControl()
    rep = await r.run(links=GH[:2], sources=["links"])
    assert rep["counts"] == {"dry_run_ok": 2}
    pv = rep["live_preview"]
    assert pv["would_submit"] == 0 and pv["quota_blocked"] == 2
    assert {b["by"] for b in pv["blocked"]} == {"platform:greenhouse"}
    assert any("greenhouse 25/25" in w and "quota_blocked" in w for w in pv["warning"])


async def test_dry_run_preview_counts_day_quota(tmp_path):
    r, _ad, eng = make(tmp_path)
    eng.control = FakeControl(platform_used=0, day_used=49)
    rep = await r.run(links=GH, sources=["links"])
    pv = rep["live_preview"]
    assert pv["would_submit"] == 1 and pv["quota_blocked"] == 2 and {b["by"] for b in pv["blocked"]} == {"day"}


async def test_live_quota_refusal_is_quota_blocked(tmp_path):
    r, ad, eng = make(tmp_path, live=True)
    eng.control = FakeControl()
    rep = await r.run(links=GH[:1], sources=["links"])
    assert rep["counts"] == {"quota_blocked": 1} and ad.submits == []
    assert "quota reached" in rep["quota_blocked"][0]["why"] and "live_preview" not in rep


async def test_report_maps_legacy_statuses(tmp_path):
    r, _ad, _ = make(tmp_path)
    rid = await r.store.start_run({"sources": ["links"]})
    await r.store.add_items(rid, [{"ref": GH[0], "source": "links", "status": "handed_off", "why": "x"},
                                  {"ref": GH[1], "source": "links", "status": "dry_run"}])
    await r.store.finish_run(rid, {"mode": "dry_run"})
    rep = await r.report(rid)
    assert rep["counts"] == {"dry_run_ok": 1, "needs_human": 1}
    assert (await r.report(rid, category="handed_off"))["counts"] == rep["counts"]
    assert "needs_human" in (await r.report(rid, category="handed_off"))
    assert "unknown category" in (await r.report(rid, category="bogus"))["error"]


# ------------------------------------------------------------------------------------------ plan paging
def _big_plan(n=60):
    return {"ref": "greenhouse:fict:1", "gate": "submit", "approval_id": "ap-1",
            "answers": {f"q_{i}": f"fictional answer number {i} " * 3 for i in range(n)},
            "src": {f"q_{i}": "llm" for i in range(0, n, 2)},
            "unresolved": [f"u_{i}" for i in range(8)],
            "client_tasks": [{"key": f"c_{i}", "label": "Why this fictional team? " * 4} for i in range(6)]}


def test_page_sections_pages_everything_within_budget():
    full, budget = _big_plan(), 1200
    merged: dict = {}
    cur, pages = 0, 0
    while cur is not None:
        page = output.page_sections(full, cur, budget)
        s = output.out(page, budget, "plan_application")
        d = json.loads(s)
        assert len(s) <= budget and "omitted" not in d and "trimmed" not in d
        assert d["ref"] == full["ref"] and d["approval_id"] == "ap-1" and d["total"] > 0
        assert d["next"] is None or d["next"] > cur
        for k in ("answers", "src"):
            merged.setdefault(k, {}).update(d.get(k, {}))
        for k in ("unresolved", "client_tasks"):
            merged.setdefault(k, []).extend(d.get(k, []))
        cur, pages = d["next"], pages + 1
    assert pages > 1
    assert {k: full[k] for k in merged} == merged          # complete, in order, nothing lost or repeated


def test_page_sections_small_result_unchanged_and_bad_cursor():
    small = {"ref": "r", "gate": "submit", "answers": {"a": "b"}}
    assert output.page_sections(small, 0, 4000) == small
    assert "past the end" in output.page_sections(_big_plan(), 10_000, 1200)["error"]


def test_generic_shaper_never_emits_negative_next():
    payload = {"why": ["w" * 300] * 30, "unresolved": ["u" * 300] * 30}
    d = json.loads(output.out(payload, 600))
    assert d.get("next") is None or d["next"] >= 0


async def test_plan_service_pages_from_memory_without_replanning():
    from apply_fakes import settings

    from job_apply_mcp.services import Services

    calls = []

    class Out:
        def compact(self):
            return _big_plan()

    class Eng:
        async def plan(self, ref):
            calls.append(ref)
            return Out()

    svc = Services(settings(response_budget_chars=1200), engine=Eng())
    p1 = await svc.plan("greenhouse:fict:1")
    assert p1["next"] and p1["total"]
    p2 = await svc.plan("greenhouse:fict:1", p1["next"])
    assert calls == ["greenhouse:fict:1"] and set(p2.get("answers", {})).isdisjoint(p1["answers"])
    with pytest.raises(ValueError, match="cursor=0 first"):
        await svc.plan("greenhouse:other:2", 5)
    with pytest.raises(ValueError, match="cursor must be"):
        await svc.plan("greenhouse:fict:1", -1)

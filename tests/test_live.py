"""Opt-in LIVE smoke tests: read-only GET / POST-search against real public endpoints. Never submits anything.

Skipped unless JAM_LIVE=1. A single network failure is not a code bug, so transport-level failures (timeouts,
5xx, 429) skip the test after one retry; a shape mismatch (missing keys, empty parse) fails it.
    JAM_LIVE=1 uv run pytest tests/test_live.py -q
"""
from __future__ import annotations

import os

import pytest

from job_apply_mcp.platforms import AdapterError, HttpClient
from job_apply_mcp.platforms.ashby import AshbyAdapter
from job_apply_mcp.platforms.greenhouse import GreenhouseAdapter
from job_apply_mcp.platforms.lever import LeverAdapter
from job_apply_mcp.platforms.smartrecruiters import SmartRecruitersAdapter
from job_apply_mcp.platforms.workday import WorkdayAdapter

pytestmark = pytest.mark.skipif(os.environ.get("JAM_LIVE") != "1", reason="live test: set JAM_LIVE=1")

# (adapter class, board, query used for the Workday search)
GH, LEVER, ASHBY, SR, WD = "airbnb", "spotify", "ramp", "ServiceNow", "nvidia.wd5/NVIDIAExternalCareerSite"


@pytest.fixture
async def live(tmp_path):
    c = HttpClient(cache_dir=tmp_path / "cache", ttl_s=0, retries=1, backoff_s=1.0, timeout_s=30)
    yield c
    await c.aclose()


async def _first(adapter, board, n=1, **kw):
    out = []
    try:
        async for p in adapter.list_postings(board, **kw):
            out.append(p)
            if len(out) >= n:
                break
    except AdapterError as e:  # network/degraded-site errors are not shape bugs
        if any(s in str(e) for s in ("HTTP 5", "HTTP 429", "timed out", "Timeout", "Connect")):
            pytest.skip(f"site degraded: {e}")
        raise
    return out


def _check(p, platform):
    assert p.platform == platform and p.ref.startswith(platform + ":")
    assert p.title and p.url.startswith("http") and p.apply_url.startswith("http")
    assert "<" not in p.title and "&lt;" not in (p.description or "")


async def test_greenhouse_live(live):
    a = GreenhouseAdapter(live, env={})
    (p,) = await _first(a, GH, with_description=True)
    _check(p, "greenhouse")
    assert p.description and "<div" not in p.description  # HTML-escaped `content` must be decoded then stripped
    got = await a.get_posting(p.ref)
    assert got.ref == p.ref
    form = await a.get_form(p.ref)
    assert any(f.kind.value == "file" for f in form.fields) and form.submit_via == ["browser"]


async def test_lever_live(live):
    a = LeverAdapter(live, env={})
    (p,) = await _first(a, LEVER, with_description=True)
    _check(p, "lever")
    assert p.description
    assert (await a.get_posting(p.ref)).ref == p.ref


async def test_ashby_live(live):
    a = AshbyAdapter(live, env={})
    (p,) = await _first(a, ASHBY, with_description=True)
    _check(p, "ashby")
    assert p.description
    assert (await a.get_posting(p.ref)).ref == p.ref
    form = await a.get_form(p.ref)  # non-user GraphQL; shape may drift, so assert only on success
    assert form.fields and any(f.required for f in form.fields)


async def test_smartrecruiters_live(live):
    a = SmartRecruitersAdapter(live, env={})
    (p,) = await _first(a, SR)
    _check(p, "smartrecruiters")
    got = await a.get_posting(p.ref, with_description=True)
    assert got.ref == p.ref and got.description


async def test_workday_live(live):
    a = WorkdayAdapter(live, env={})
    ps = await _first(a, WD, n=3, query="engineer")
    assert ps
    _check(ps[0], "workday")
    got = await a.get_posting(ps[0].ref, with_description=True)
    assert got.title and got.description
    # URL form from a public posting page resolves to the same board
    assert a.parse(f"https://nvidia.wd5.myworkdayjobs.com/en-US/NVIDIAExternalCareerSite/job/{ps[0].ref.split(':', 2)[2]}")[0] == WD

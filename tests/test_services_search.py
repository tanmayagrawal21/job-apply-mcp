import json

import httpx
import platform_fixtures as fx
import respx

from job_apply_mcp.config import Settings
from job_apply_mcp.services import Services


def svc(tmp_path, **policy):
    s = Settings.model_validate({
        "paths": {"data_dir": str(tmp_path)}, "policy": policy,
        "concurrency": {"min_interval_s": 0},
        "platforms": {"backoff_s": 0, "boards": {"greenhouse": ["acmeco"], "lever": ["democo"], "ashby": ["exampleai"]}},
    })
    return Services(s)


def mock_all():
    respx.get("https://boards-api.greenhouse.io/v1/boards/acmeco/jobs").respond(json=fx.GH_LIST)
    respx.get("https://api.lever.co/v0/postings/democo").respond(json=fx.LEVER_LIST)
    respx.get("https://api.ashbyhq.com/posting-api/job-board/exampleai").respond(json=fx.ASHBY_BOARD)


@respx.mock
async def test_fanout_compact_rows_and_pagination(tmp_path):
    mock_all()
    s = svc(tmp_path)
    out = await s.search(since_days=0, apply_policy=False, limit=2)
    assert out["total"] == 4 and out["next"] == 2 and len(out["rows"]) == 2
    assert set(out["rows"][0]) <= {"ref", "co", "title", "loc", "pay", "flags", "d", "score", "why"}
    assert "description" not in json.dumps(out)
    assert out["rows"][0]["d"] >= out["rows"][1]["d"]  # newest first
    out2 = await s.search(since_days=0, apply_policy=False, limit=2, cursor=out["next"])
    assert out2["next"] is None and {r["ref"] for r in out["rows"]}.isdisjoint(r["ref"] for r in out2["rows"])
    await s.aclose()


@respx.mock
async def test_query_location_since_and_error_isolation(tmp_path):
    mock_all()
    respx.get("https://api.ashbyhq.com/posting-api/job-board/gone").respond(404)
    s = svc(tmp_path)
    out = await s.search(query="engineer", boards=["greenhouse:acmeco", "lever:democo", "ashby:gone"], since_days=0, apply_policy=False)
    assert {r["co"] for r in out["rows"]} == {"Acme Co", "democo"}
    assert out["errors"][0]["board"] == "ashby:gone"  # one bad board does not fail the batch
    out = await s.search(location="remote", boards=["greenhouse:acmeco"], since_days=0, apply_policy=False)
    assert [r["loc"] for r in out["rows"]] == ["Remote, US"]
    out = await s.search(boards=["greenhouse:acmeco"], since_days=7)
    assert all(r["d"] >= "2026-01-03" for r in out["rows"])
    await s.aclose()


@respx.mock
async def test_policy_prefilter_uses_textflags(tmp_path):
    mock_all()
    s = svc(tmp_path, requires_sponsorship=True, max_years_required=2, graduation="2026-05")
    out = await s.search(since_days=0)
    refs = {r["ref"] for r in out["rows"]}
    assert "greenhouse:acmeco:101" not in refs   # no sponsorship
    assert f"lever:democo:{fx.LEVER_ID}" not in refs  # citizenship
    assert f"ashby:exampleai:{fx.ASHBY_ID}" not in refs  # grad window Fall 2027
    assert out["filtered"] == 3 and "greenhouse:acmeco:102" in refs
    await s.aclose()


@respx.mock
async def test_get_job_and_get_form_resolve_refs_and_urls(tmp_path):
    respx.get("https://boards-api.greenhouse.io/v1/boards/acmeco/jobs/101").mock(side_effect=lambda req: httpx.Response(
        200, json=fx.GH_JOB if req.url.params.get("questions") else fx.GH_LIST["jobs"][0]))
    s = svc(tmp_path)
    j = await s.get_job("https://job-boards.greenhouse.io/acmeco/jobs/101")
    assert j["ref"] == "greenhouse:acmeco:101" and "description" not in j and "no_sponsorship" in j["flags"]
    j = await s.get_job("greenhouse:acmeco:101", fields=["description", "apply_url", "flags_detail"])
    assert j["description"].startswith("Build") and j["apply_url"] and isinstance(j["flags"], dict)
    f = await s.get_form("greenhouse:acmeco:101")
    assert f["via"] == ["browser"] and {"k": "first_name", "l": "First Name", "t": "text", "r": 1} in f["fields"]
    assert len(json.dumps(f)) < 1500
    import pytest

    from job_apply_mcp.platforms import AdapterError
    with pytest.raises(AdapterError, match="no enabled platform"):
        await s.get_job("https://example.com/jobs/1")
    await s.aclose()


async def test_no_boards_hint(tmp_path):
    s = Services(Settings.model_validate({"paths": {"data_dir": str(tmp_path)}}))
    out = await s.search()
    assert out["rows"] == [] and "boards" in out["hint"]

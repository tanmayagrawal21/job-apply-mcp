import httpx
import platform_fixtures as fx
import pytest
import respx

from job_apply_mcp.config import PlatformsCfg
from job_apply_mcp.core import registry
from job_apply_mcp.core.models import FieldKind
from job_apply_mcp.platforms import AdapterError, build_adapters, detect
from job_apply_mcp.platforms.ashby import AshbyAdapter
from job_apply_mcp.platforms.greenhouse import GreenhouseAdapter
from job_apply_mcp.platforms.jobspy_adapter import JobSpyAdapter
from job_apply_mcp.platforms.lever import LeverAdapter
from job_apply_mcp.platforms.smartrecruiters import SmartRecruitersAdapter
from job_apply_mcp.platforms.workday import WorkdayAdapter, _posted


async def collect(ait):
    return [p async for p in ait]


def test_all_adapters_registered():
    assert {"greenhouse", "lever", "ashby", "smartrecruiters", "workday", "jobspy"} <= set(registry.platforms.names())


async def test_detect_urls(http):
    ads = build_adapters(["greenhouse", "lever", "ashby", "smartrecruiters", "workday", "jobspy", "generic"], http)
    assert "generic" not in ads  # not registered yet: skipped, not an error
    cases = {
        "https://job-boards.greenhouse.io/acmeco/jobs/101": "greenhouse",
        "https://careers.example.com/open?gh_jid=555": "greenhouse",
        "https://jobs.lever.co/democo/" + fx.LEVER_ID: "lever",
        "https://jobs.ashbyhq.com/exampleai/" + fx.ASHBY_ID: "ashby",
        "https://jobs.smartrecruiters.com/samplecorp/7430001-data-analyst": "smartrecruiters",
        "https://demo.wd5.myworkdayjobs.com/en-US/External/job/Phoenix-AZ/X_JR1": "workday",
        "https://www.linkedin.com/jobs/view/123": "jobspy",
    }
    for url, name in cases.items():
        assert detect(ads, url).name == name, url
    assert detect(ads, "https://example.com/careers") is None


# ----------------------------------------------------------------------------------------------- greenhouse
@respx.mock
async def test_greenhouse_list_get_form(http):
    a = GreenhouseAdapter(http, env={})
    respx.get("https://boards-api.greenhouse.io/v1/boards/acmeco/jobs", params={"content": "true"}).respond(json=fx.GH_LIST)
    ps = await collect(a.list_postings("acmeco", since="2026-10-01"))
    assert [p.ref for p in ps] == ["greenhouse:acmeco:101"]  # old posting filtered by `since`
    p = ps[0]
    assert p.company == "Acme Co" and p.salary == "$120,000-$150,000" and p.description is None
    assert {"no_sponsorship", "years"} <= set(p.flags)
    assert (await collect(a.list_postings("acmeco", with_description=True)))[0].description.startswith("Build services")

    respx.get("https://boards-api.greenhouse.io/v1/boards/acmeco/jobs/101", params={"questions": "true"}).respond(json=fx.GH_JOB)
    form = await a.get_form("https://job-boards.greenhouse.io/acmeco/jobs/101")
    kinds = {f.key: (f.kind, f.required) for f in form.fields}
    assert kinds["first_name"] == (FieldKind.text, True)
    assert kinds["resume"] == (FieldKind.file, True) and kinds["resume_text"] == (FieldKind.textarea, False)
    assert kinds["question_1"] == (FieldKind.boolean, True) and kinds["question_2"][0] == FieldKind.multiselect
    assert form.submit_via == ["browser"] and form.ref == "greenhouse:acmeco:101"


@respx.mock
async def test_greenhouse_get_posting_404(http):
    respx.get("https://boards-api.greenhouse.io/v1/boards/acmeco/jobs/9").respond(404)
    with pytest.raises(AdapterError, match="not found"):
        await GreenhouseAdapter(http, env={}).get_posting("greenhouse:acmeco:9")


@respx.mock
async def test_greenhouse_submit_paths(http, tmp_path):
    q = respx.get("https://boards-api.greenhouse.io/v1/boards/acmeco/jobs/101", params={"questions": "true"}).respond(json=fx.GH_JOB)
    plain = GreenhouseAdapter(http, env={})
    form = await plain.get_form("greenhouse:acmeco:101")
    r = await plain.submit(form, {}, {}, dry_run=False)
    assert r.status == "needs_browser"  # no key: never attempts anything

    a = GreenhouseAdapter(http, env={"JOB_APPLY_KEY_GREENHOUSE_ACMECO": "secret"})
    form = await a.get_form("greenhouse:acmeco:101")
    assert form.submit_via == ["api", "browser"]
    assert (await a.submit(form, {"first_name": "Ada"}, {}, dry_run=False)).status == "needs_human"  # missing required
    resume = tmp_path / "cv.pdf"
    resume.write_bytes(b"%PDF-1.4 fake")
    answers = {"first_name": "Ada", "question_1": "No", "question_2": ["Python", "Go"]}
    assert (await a.submit(form, answers, {"resume": str(resume)}, dry_run=True)).status == "dry_run"
    post = respx.post("https://boards-api.greenhouse.io/v1/boards/acmeco/jobs/101").respond(json={"success": "Application submitted"})
    r = await a.submit(form, answers, {"resume": str(resume)}, dry_run=False)
    assert r.status == "submitted" and r.via == "api"
    req = post.calls.last.request
    assert req.headers["authorization"].startswith("Basic ")
    body = req.content.decode(errors="ignore")
    assert 'name="question_1"' in body and "\r\n0\r\n" in body  # "No" mapped to value 0
    assert q.called
    respx.post("https://boards-api.greenhouse.io/v1/boards/acmeco/jobs/101").respond(400, json={"error": "bad"})
    assert (await a.submit(form, answers, {"resume": str(resume)}, dry_run=False)).status == "error"


# ---------------------------------------------------------------------------------------------------- lever
@respx.mock
async def test_lever(http):
    a = LeverAdapter(http, env={})
    respx.get("https://api.lever.co/v0/postings/democo", params={"mode": "json"}).respond(json=fx.LEVER_LIST)
    (p,) = await collect(a.list_postings("democo"))
    assert p.ref == f"lever:democo:{fx.LEVER_ID}" and p.salary == "$140,000-$170,000" and p.posted_at == "2025-10-09"
    assert "citizenship" in p.flags and "Go" not in (p.description or "")
    respx.get(f"https://api.lever.co/v0/postings/democo/{fx.LEVER_ID}").respond(json=fx.LEVER_LIST[0])
    got = await a.get_posting(f"https://jobs.lever.co/democo/{fx.LEVER_ID}/apply", with_description=True)
    assert "Requirements: Go SQL" in got.description
    respx.get(f"https://jobs.lever.co/democo/{fx.LEVER_ID}/apply").respond(text="<html><form></form></html>")
    form = await a.get_form(got.ref)
    assert {"name", "email", "resume"} <= {f.key for f in form.fields} and form.submit_via == ["browser"]
    assert (await a.submit(form, {}, {}, dry_run=False)).status == "needs_browser"


@respx.mock
async def test_lever_api_submit_never_leaks_key(http, tmp_path):
    a = LeverAdapter(http, env={"JOB_APPLY_KEY_LEVER_DEMOCO": "k123"})
    respx.get(f"https://jobs.lever.co/democo/{fx.LEVER_ID}/apply").respond(text="<html><form></form></html>")  # ADR-0074
    form = await a.get_form(f"lever:democo:{fx.LEVER_ID}")
    cv = tmp_path / "cv.pdf"
    cv.write_bytes(b"x")
    answers = {"name": "Ada Example", "email": "ada@example.test"}
    assert (await a.submit(form, answers, {"resume": str(cv)}, dry_run=True)).status == "dry_run"
    route = respx.post(f"https://api.lever.co/v0/postings/democo/{fx.LEVER_ID}").respond(json={"ok": True, "applicationId": "app-1"})
    r = await a.submit(form, answers, {"resume": str(cv)}, dry_run=False)
    assert (r.status, r.confirmation) == ("submitted", "app-1") and route.calls.last.request.url.params["key"] == "k123"
    respx.post(f"https://api.lever.co/v0/postings/democo/{fx.LEVER_ID}").respond(401, json={"error": "nope"})
    r = await a.submit(form, answers, {"resume": str(cv)}, dry_run=False)
    assert r.status == "error" and "k123" not in (r.detail or "")


# --------------------------------------------------------------------------------------------------- ashby
@respx.mock
async def test_ashby(http):
    a = AshbyAdapter(http)
    respx.get("https://api.ashbyhq.com/posting-api/job-board/exampleai", params={"includeCompensation": "true"}).respond(json=fx.ASHBY_BOARD)
    ps = await collect(a.list_postings("exampleai"))
    assert [p.ref for p in ps] == [f"ashby:exampleai:{fx.ASHBY_ID}"]  # unlisted dropped
    assert ps[0].salary == "$150K - $200K" and "grad_window" in ps[0].flags
    assert (await a.get_posting(f"https://jobs.ashbyhq.com/exampleai/{fx.ASHBY_ID}/application")).title == "ML Engineer"
    with pytest.raises(AdapterError):
        await a.get_posting("ashby:exampleai:nope")
    gql = respx.post("https://jobs.ashbyhq.com/api/non-user-graphql", params={"op": "ApiJobPosting"}).respond(json=fx.ASHBY_FORM)
    form = await a.get_form(ps[0].ref)
    assert gql.calls.last.request.read().decode().count(fx.ASHBY_ID) == 1
    by = {f.key: f for f in form.fields}
    assert by["_systemfield_resume"].kind == FieldKind.file and by["_systemfield_name"].required
    assert by["q1"].options == ["Yes", "No"] and form.submit_via == ["browser"]
    assert (await a.submit(form, {}, {}, dry_run=False)).status == "needs_browser"
    gql.respond(json={"errors": [{"message": "x"}], "data": None})
    a2 = AshbyAdapter(http)
    with pytest.raises(AdapterError):
        await a2.get_form("ashby:exampleai:other-id")


# ------------------------------------------------------------------------------------------- smartrecruiters
@respx.mock
async def test_smartrecruiters(http):
    a = SmartRecruitersAdapter(http)
    respx.get("https://api.smartrecruiters.com/v1/companies/samplecorp/postings", params={"limit": "100", "offset": "0"}).respond(json=fx.SR_LIST)
    (p,) = await collect(a.list_postings("samplecorp"))
    assert p.ref == "smartrecruiters:samplecorp:7430001" and p.company == "Sample Retail" and p.location.startswith("Austin")
    respx.get("https://api.smartrecruiters.com/v1/companies/samplecorp/postings/7430001").respond(json=fx.SR_JOB)
    got = await a.get_posting("https://jobs.smartrecruiters.com/samplecorp/7430001-data-analyst", with_description=True)
    assert got.salary == "$30-$40/hr" and "Analyze data" in got.description and got.apply_url.endswith("oga=true")
    form = await a.get_form(got.ref)
    assert form.submit_via == ["browser"] and (await a.submit(form, {}, {}, dry_run=False)).status == "needs_browser"


@respx.mock
async def test_smartrecruiters_pagination(http):
    pages = {0: {"totalFound": 150, "content": [{"id": str(i), "name": "t", "company": {}} for i in range(100)]},
             100: {"totalFound": 150, "content": [{"id": str(i), "name": "t", "company": {}} for i in range(100, 150)]}}
    respx.get("https://api.smartrecruiters.com/v1/companies/big/postings").mock(
        side_effect=lambda req: httpx.Response(200, json=pages[int(req.url.params["offset"])]))
    assert len(await collect(SmartRecruitersAdapter(http).list_postings("big"))) == 150


# --------------------------------------------------------------------------------------------------- workday
@respx.mock
async def test_workday(http):
    a = WorkdayAdapter(http, PlatformsCfg(workday_max_pages=3))
    url = "https://demo.wd5.myworkdayjobs.com/en-US/External/job/Phoenix-AZ/Associate-Engineer_JR100/apply"
    assert a.parse(url) == ("demo.wd5/External", "Phoenix-AZ/Associate-Engineer_JR100")
    assert a.parse("https://demo.wd5.myworkdayjobs.com/External") == ("demo.wd5/External", None)
    route = respx.post("https://demo.wd5.myworkdayjobs.com/wday/cxs/demo/External/jobs").respond(json=fx.WD_SEARCH)
    ps = await collect(a.list_postings("demo.wd5/External", query="engineer"))
    assert len(ps) == 2 and route.call_count == 1  # total=2 < page size: one page only
    assert b'"searchText":"engineer"' in route.calls.last.request.content.replace(b" ", b"")
    assert ps[0].ref == "workday:demo.wd5/External:Associate-Engineer_JR100".replace(":Associate", ":Phoenix-AZ/Associate")
    assert ps[0].apply_url.endswith("/apply") and ps[0].posted_at
    respx.get("https://demo.wd5.myworkdayjobs.com/wday/cxs/demo/External/job/Phoenix-AZ/Associate-Engineer_JR100").respond(json=fx.WD_JOB)
    got = await a.get_posting(url)
    assert got.company == "Demo Industries" and "citizenship" in got.flags and got.posted_at == "2026-10-19"
    form = await a.get_form(got.ref)
    assert form.fields == [] and (await a.submit(form, {}, {}, dry_run=False)).status == "needs_browser"


@respx.mock
async def test_workday_pagination_uses_first_page_total(http):
    def side(req):
        import json as _j
        off = _j.loads(req.content)["offset"]
        rows = [{"title": f"t{i}", "externalPath": f"/job/L/t{i}_R{i}"} for i in range(off, min(off + 20, 45))]
        return httpx.Response(200, json={"total": 45 if off == 0 else 0, "jobPostings": rows})
    respx.post("https://demo.wd5.myworkdayjobs.com/wday/cxs/demo/External/jobs").mock(side_effect=side)
    assert len(await collect(WorkdayAdapter(http).list_postings("demo.wd5/External"))) == 45


def test_workday_relative_dates():
    from datetime import date
    t = date(2026, 10, 22)
    assert _posted("Posted Today", t) == "2026-10-22" and _posted("Posted Yesterday", t) == "2026-10-21"
    assert _posted("Posted 3 Days Ago", t) == "2026-10-19" and _posted(None, t) is None


# --------------------------------------------------------------------------------------------------- jobspy
class _Frame:
    def __init__(self, rows):
        self.rows = rows

    def to_dict(self, _orient):
        return self.rows


async def test_jobspy_discovery_only(http, monkeypatch):
    rows = [{"id": "li-1", "site": "linkedin", "job_url": "https://www.linkedin.com/jobs/view/1", "title": "Data Engineer",
             "company": "Demo Co", "location": "Remote", "date_posted": "2026-10-20", "min_amount": 100000.0,
             "max_amount": float("nan"), "currency": "USD", "description": "Pipelines. No sponsorship available.",
             "job_url_direct": None}]
    seen = {}
    monkeypatch.setattr(JobSpyAdapter, "_scrape", staticmethod(lambda **kw: seen.update(kw) or _Frame(rows)))
    a = JobSpyAdapter(http)
    (p,) = await collect(a.list_postings("linkedin,indeed", query="data engineer", location="Remote"))
    assert seen["site_name"] == ["linkedin", "indeed"] and seen["search_term"] == "data engineer"
    assert p.ref == "jobspy:linkedin:li-1" and p.salary is None and "no_sponsorship" in p.flags
    with pytest.raises(AdapterError, match="unknown jobspy"):
        await collect(a.list_postings("myspace"))
    with pytest.raises(AdapterError):
        await a.get_form("jobspy:linkedin:li-1")
    form = type("F", (), {"ref": p.ref})()
    assert (await a.submit(form, {}, {}, dry_run=False)).status == "needs_human"


async def test_jobspy_missing_extra_message(http, monkeypatch):
    import sys
    monkeypatch.setitem(sys.modules, "jobspy", None)
    with pytest.raises(AdapterError, match="--extra jobspy"):
        await collect(JobSpyAdapter(http).list_postings("linkedin"))

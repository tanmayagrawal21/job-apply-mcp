"""Issue #127 / ADR-0074: a failed form fetch fails closed and is reported as a categorised tool_gap.
Fictional boards and postings only; respx mocks every request, nothing touches the network."""
import httpx
import pytest
import respx
from apply_fakes import POSTING, FakeAdapter, FakeProfile, FakeStore
from lever_card_fixtures import page
from platform_fixtures import ASHBY_ID, LEVER_ID, LEVER_LIST
from test_apply_engine import engine

from job_apply_mcp.apply.engine import ApplyEngine
from job_apply_mcp.core.handoff import categorize
from job_apply_mcp.core.models import Decision
from job_apply_mcp.platforms import AdapterError, PostingClosed
from job_apply_mcp.platforms.ashby import AshbyAdapter
from job_apply_mcp.platforms.greenhouse import GreenhouseAdapter
from job_apply_mcp.platforms.lever import LeverAdapter
from job_apply_mcp.services import Services

APPLY = f"https://jobs.lever.co/democo/{LEVER_ID}/apply"
REF = f"lever:democo:{LEVER_ID}"


# ------------------------------------------------------------------------------------------------ adapters
@respx.mock
@pytest.mark.parametrize("fail", ["503", "timeout", "boom"])
async def test_lever_failed_apply_page_is_tool_gap_and_never_posts(http, fail):
    route = respx.get(APPLY)
    if fail == "503":
        route.respond(status_code=503)
    elif fail == "timeout":
        route.mock(side_effect=httpx.ReadTimeout("slow"))
    else:
        route.mock(side_effect=RuntimeError("unexpected"))
    post = respx.post(url__regex=r".*").respond(json={"applicationId": "x"})
    a = LeverAdapter(http, env={"JOB_APPLY_KEY_LEVER_DEMOCO": "test-key"})
    with pytest.raises(AdapterError, match=r"^tool_gap: could not read the Lever apply page") as ei:
        await a.get_form(REF)
    assert not isinstance(ei.value, PostingClosed)
    assert not post.called
    if fail == "503":
        assert route.call_count == 3                     # the client's bounded retries ran first


@respx.mock
@pytest.mark.parametrize("status", [404, 410])
async def test_lever_apply_page_gone_is_closed(http, status):
    respx.get(APPLY).respond(status_code=status)
    with pytest.raises(PostingClosed):
        await LeverAdapter(http, env={}).get_form(REF)


@respx.mock
async def test_lever_apply_page_ok_still_returns_cards(http):
    respx.get(APPLY).respond(text=page())
    form = await LeverAdapter(http, env={}).get_form(REF)
    assert len(form.fields) > 4


@respx.mock
async def test_greenhouse_timeout_and_5xx_are_tool_gap_404_closed(http):
    base = "https://boards-api.greenhouse.io/v1/boards/acmeco/jobs"
    respx.get(f"{base}/1").mock(side_effect=httpx.ConnectTimeout("slow"))
    respx.get(f"{base}/2").respond(status_code=502)
    respx.get(f"{base}/3").respond(status_code=404)
    a = GreenhouseAdapter(http, env={})
    for jid in ("1", "2"):
        with pytest.raises(AdapterError, match="^tool_gap:") as ei:
            await a.get_form(f"greenhouse:acmeco:{jid}")
        assert not isinstance(ei.value, PostingClosed)
    with pytest.raises(PostingClosed):
        await a.get_form("greenhouse:acmeco:3")


@respx.mock
async def test_ashby_timeout_is_tool_gap(http):
    respx.post(url__regex=r"https://jobs\.ashbyhq\.com/api/non-user-graphql.*").mock(
        side_effect=httpx.ReadTimeout("slow"))
    with pytest.raises(AdapterError, match="^tool_gap:") as ei:
        await AshbyAdapter(http, env={}).get_form(f"ashby:exampleai:{ASHBY_ID}")
    assert not isinstance(ei.value, PostingClosed)


# ------------------------------------------------------------------------------------------------ engine
class _GapAdapter(FakeAdapter):
    async def get_form(self, ref):
        raise AdapterError("tool_gap: could not read the Lever apply page (HTTP 503), so its custom questions are "
                           "unknown; the form is not reported")


class _PlainErrAdapter(FakeAdapter):
    async def get_form(self, ref):
        raise AdapterError("HTTP 500 for https://example.test/form")


async def test_plan_returns_categorised_tool_gap():
    e, ad = engine(adapter=_GapAdapter())
    out = await e.plan(POSTING.ref)
    assert out.plan.gate.decision == Decision.needs_human
    d = out.compact()
    assert d["category"] == "tool_gap" and d["reason"].startswith("tool_gap: could not read")
    assert not out.plan.answers and not ad.submits
    e2, _ = engine(adapter=_PlainErrAdapter())         # an untagged AdapterError gets the tool_gap tag
    assert (await e2.plan(POSTING.ref)).compact()["reason"] == "tool_gap: HTTP 500 for https://example.test/form"


async def test_submit_returns_categorised_tool_gap_and_never_submits():
    e, ad = engine(adapter=_GapAdapter())
    for dry in (True, False):
        r = await e.submit(POSTING.ref, {"email": "a@example.test"}, dry_run=dry)
        assert r.status == "needs_human" and r.detail.startswith("tool_gap:")
    assert not ad.submits
    assert categorize(r.status, r.detail) == "tool_gap"


async def test_closed_still_skipped_not_tool_gap():
    class _Closed(FakeAdapter):
        async def get_form(self, ref):
            raise PostingClosed("apply page not found (closed or moved)")
    e, _ = engine(adapter=_Closed())
    out = await e.plan(POSTING.ref)
    assert out.plan.gate.decision == Decision.skip and "category" not in out.compact()


async def test_apply_batch_records_tool_gap_and_continues():
    good, gap = FakeAdapter(), _GapAdapter()
    e = ApplyEngine(engine()[0].s, resolve_adapter=lambda r: gap if r.endswith(":9") else good,
                    profile=FakeProfile(), router=None, gates=engine()[0].gates, store=FakeStore(),
                    driver_factory=lambda n: None)
    rows = await e.apply_batch(["greenhouse:acme:9", POSTING.ref], dry_run=True)
    by = {r["ref"]: r for r in rows}
    assert by["greenhouse:acme:9"]["status"] == "needs_human"
    assert by["greenhouse:acme:9"]["category"] == "tool_gap" and by["greenhouse:acme:9"]["reason"]
    assert by[POSTING.ref]["status"] != "error"          # the other item still ran
    assert not gap.submits


# ------------------------------------------------------------------------------------------------ services
async def test_services_plan_and_get_form_are_categorised(tmp_path):
    e, _ = engine(adapter=_GapAdapter())
    svc = Services(e.s, engine=e)
    svc._adapters = {"greenhouse": _GapAdapter()}
    d = await svc.plan(POSTING.ref)
    assert d["status"] == "needs_human" and d["category"] == "tool_gap" and d["cat"] == "tool_gap"
    assert d["reason"].startswith("tool_gap:")
    f = await svc.get_form(POSTING.ref)
    assert f["status"] == "needs_human" and f["category"] == "tool_gap" and f["cat"] == "tool_gap"
    assert f["reason"].startswith("tool_gap:") and "fields" not in f


@respx.mock
async def test_services_get_form_lever_503_end_to_end(http):
    respx.get(APPLY).respond(status_code=503)
    svc = Services(engine()[0].s, engine=engine()[0])
    svc._adapters = {"lever": LeverAdapter(http, env={})}
    f = await svc.get_form(REF)
    assert f["status"] == "needs_human" and f["category"] == "tool_gap" and f["reason"]
    respx.get(APPLY).respond(status_code=404)
    assert (await svc.get_form(REF))["status"] == "skipped"


@respx.mock
async def test_engine_plan_lever_timeout_end_to_end(http):
    respx.get(f"https://api.lever.co/v0/postings/democo/{LEVER_ID}").respond(json=LEVER_LIST[0])
    respx.get(APPLY).mock(side_effect=httpx.ReadTimeout("slow"))
    post = respx.post(url__regex=r".*").respond(json={"applicationId": "x"})
    e, _ = engine(adapter=LeverAdapter(http, env={"JOB_APPLY_KEY_LEVER_DEMOCO": "test-key"}))
    out = await e.plan(REF)
    assert out.compact()["category"] == "tool_gap"
    r = await e.submit(REF, {"name": "A Tester"}, dry_run=False)
    assert r.status == "needs_human" and r.detail.startswith("tool_gap:")
    assert not post.called


async def test_run_applications_records_tool_gap_and_continues(tmp_path):
    from test_intake import MultiAdapter, make

    class _OneGap(MultiAdapter):
        async def get_form(self, ref):
            if "/gapco/" in ref:
                raise AdapterError("tool_gap: could not read the Lever apply page (HTTP 503)")
            return await super().get_form(ref)

    r, ad, _ = make(tmp_path, adapter=_OneGap())
    rep = await r.run(links=["https://boards.greenhouse.io/gapco/jobs/1", "https://boards.greenhouse.io/okco/jobs/2"],
                      sources=["links"])
    gap = [x for x in rep.get("needs_human", []) if "gapco" in x["ref"]]
    assert gap and gap[0]["cat"] == "tool_gap" and gap[0]["why"].startswith("tool_gap:")
    assert rep["handoff_categories"].get("tool_gap") == 1
    assert "failed" not in rep["counts"]
    assert not any("gapco" in s for s in ad.submits)
    assert rep["counts"].get("dry_run_ok") == 1                 # the other posting still ran


# ------------------------------------------------------------------------------------------------ review of #130
async def test_tool_gap_takes_no_run_slot(tmp_path):
    """ADR-0049 / ADR-0074: a fetch tool_gap must not use up max_applications; the ready posting still runs."""
    from test_intake import MultiAdapter, make

    class _OneGap(MultiAdapter):
        async def get_form(self, ref):
            if "/gapco/" in ref:
                raise AdapterError("tool_gap: could not read the Lever apply page (HTTP 503)")
            return await super().get_form(ref)

    r, ad, _ = make(tmp_path, adapter=_OneGap())
    rep = await r.run(links=["https://boards.greenhouse.io/gapco/jobs/1", "https://boards.greenhouse.io/okco/jobs/2"],
                      sources=["links"], max_applications=1)
    assert rep["counts"].get("dry_run_ok") == 1 and rep["counts"].get("needs_human") == 1
    assert "skipped" not in rep["counts"]
    assert any("okco" in s for s in ad.submits)


@respx.mock
async def test_lever_posting_api_timeout_is_tool_gap(http):
    respx.get(f"https://api.lever.co/v0/postings/democo/{LEVER_ID}").mock(side_effect=httpx.ReadTimeout("slow"))
    respx.get(APPLY).respond(text=page())
    a = LeverAdapter(http, env={})
    with pytest.raises(AdapterError, match="^tool_gap: could not read the Lever postings API") as ei:
        await a.get_posting(REF)
    assert not isinstance(ei.value, PostingClosed)
    e, _ = engine(adapter=a)
    d = (await e.plan(REF)).compact()
    assert d["category"] == "tool_gap" and d["reason"].startswith("tool_gap:")


@respx.mock
async def test_malformed_lever_url_is_invalid_ref_not_tool_gap(http):
    from job_apply_mcp.platforms import InvalidRef
    bad = "https://jobs.lever.co/"                      # no board, no posting
    a = LeverAdapter(http, env={})
    with pytest.raises(InvalidRef, match="not a Lever URL"):
        await a.get_form(bad)
    with pytest.raises(InvalidRef, match="names a board"):
        await a.get_form("lever:democo:")
    e, _ = engine(adapter=a)
    with pytest.raises(InvalidRef):
        await e.plan(bad)
    r = await e.submit(bad, {}, dry_run=True)
    assert r.status == "error" and r.detail.startswith("invalid ref: not a Lever URL")
    assert categorize(r.status, r.detail) is None
    rows = await e.apply_batch([bad], dry_run=True)
    assert rows[0]["status"] == "error" and rows[0]["error"] == "invalid_ref" and "category" not in rows[0]
    svc = Services(e.s, engine=e)
    svc._adapters = {"lever": a}
    for d in (await svc.get_form(bad), await svc.plan(bad)):
        assert d["status"] == "error" and d["error"] == "invalid_ref" and "not a Lever URL" in d["reason"]
        assert "category" not in d and "cat" not in d and d["next_action"] == "fix_input"


# ------------------------------------------------------------------------------------------------ privacy (#130)
@respx.mock
@pytest.mark.parametrize("bad", [
    "https://user:pass" + "@" + "jobs.lever.co/?token=abc123&x=1#frag",
    "https://user:pass" + "@" + "example.test/careers?token=abc123&x=1#frag",
])
async def test_invalid_ref_never_echoes_userinfo_query_or_fragment(http, bad):
    from job_apply_mcp.platforms import InvalidRef
    with pytest.raises(InvalidRef) as ei:
        await LeverAdapter(http, env={}).get_form(bad)
    msg = str(ei.value)
    assert "not a Lever URL" in msg
    for leak in ("user", "pass", "token", "abc123", "x=1", "frag", "?", "#", "@"):
        assert leak not in msg, (leak, msg)


@pytest.mark.parametrize("make_bad", [
    lambda: "greenhouse:acmeco",                                       # too few parts
    lambda: "lever::",                                                 # empty board
])
@respx.mock                                                            # any request would fail the test
async def test_non_url_malformed_ref_is_echoed_safely(http, make_bad):
    from job_apply_mcp.core import secrets
    from job_apply_mcp.platforms import InvalidRef
    secret = "s3cr3t-value-xyz"
    secrets.Secret(secret).reveal()                                    # a revealed value is redacted from then on
    try:
        ref = make_bad() + secret + "?token=abc123#frag" + "z" * 500
        a = GreenhouseAdapter(http, env={}) if ref.startswith("greenhouse") else LeverAdapter(http, env={})
        with pytest.raises(InvalidRef) as ei:
            await a.get_form(ref)
        msg = str(ei.value)
        assert secret not in msg and secrets.MASK in msg
        assert "token" not in msg and "abc123" not in msg and "frag" not in msg
        assert len(msg) <= 300 and "z" * 200 not in msg
    finally:
        secrets.forget(secret)


def test_http_error_and_tool_gap_reason_strip_userinfo_and_query():
    from job_apply_mcp.apply.engine import tool_gap_reason
    from job_apply_mcp.platforms import HttpError
    e = HttpError(503, "https://user:pass" + "@" + "jobs.lever.co/democo/x/apply?key=abc123#frag")
    assert str(e) == "HTTP 503 for https://jobs.lever.co/democo/x/apply"
    r = tool_gap_reason(AdapterError("boom at https://u:p" + "@" + "api.example.test/v0?key=abc123"))
    assert r == "tool_gap: boom at https://api.example.test/v0"

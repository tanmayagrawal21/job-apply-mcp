"""browser.final_click = "human" (ADR-0057): the agent fills and verifies, a person clicks Submit. Fakes only."""
import asyncio
import types

import pytest
from apply_fakes import POSTING, FakeAdapter, FakeGates, FakeProfile, FakeStore, settings
from test_approvals import _cfg

from job_apply_mcp.apply.browser_playwright import ATS, PlaywrightDriver
from job_apply_mcp.apply.engine import ApplyEngine
from job_apply_mcp.config import Settings
from job_apply_mcp.core.models import SubmitResult
from job_apply_mcp.intake.runner import Cand, IntakeRunner

FORM_TEXT = "Name\nEmail\nSubmit Application"
DONE_TEXT = "Thank you for applying! Your application has been received."


class _Loc:
    def __init__(self, page, sel):
        self.page, self.sel = page, sel

    async def inner_text(self):
        return self.page.text

    async def count(self):
        return 0 if self.page.done else 1

    def click(self, *a, **k):             # any click would be a bug in this mode
        self.page.clicks += 1
        raise AssertionError("final_click=human must never click")


class FakePage:
    """Just enough of a Playwright page for the handoff and the watcher. Records clicks/typing (must stay 0)."""

    def __init__(self, url="https://jobs.example-ats.test/acme/1/application"):
        self.url, self.text, self.done, self.closed = url, FORM_TEXT, False, False
        self.clicks = self.fronted = 0
        self.keyboard = types.SimpleNamespace(press=self._typed, type=self._typed)

    def _typed(self, *a, **k):
        self.clicks += 1
        raise AssertionError("final_click=human must never type")

    def locator(self, sel):
        return _Loc(self, sel)

    def get_by_role(self, role, name=None):
        return _Loc(self, role)

    def is_closed(self):
        return self.closed

    async def bring_to_front(self):
        self.fronted += 1

    def confirm(self, url="https://jobs.example-ats.test/acme/1/application/success"):
        self.url, self.text, self.done = url, DONE_TEXT, True


def _driver(headless=False, timeout=5.0):
    s = Settings()
    s.browser.final_click, s.browser.headless = "human", headless
    s.browser.human_click_poll_s, s.browser.human_click_timeout_s = 0.05, timeout
    d = PlaywrightDriver(s)

    async def ev(page, ref, stage, extra=None):
        d.stages.append(stage)
        return f" evidence=/tmp/{stage}"

    async def recs(page):
        return []
    d.stages = []
    d._save_evidence, d._records = ev, recs
    return d


async def test_handoff_keeps_the_tab_fronts_it_and_never_clicks():
    d, page = _driver(), FakePage()
    r = await d._human_handoff(page, ATS["ashby"], "ashby:acme:1")
    assert r.status == "handoff" and "click Submit in the open tab" in r.detail
    assert id(page) in d._handoff_pages and page.fronted == 1 and "ashby:acme:1" in d._watchers
    d._watchers["ashby:acme:1"].cancel()
    assert page.clicks == 0


async def test_headless_is_needs_human_with_hint_and_no_watcher():
    d, page = _driver(headless=True), FakePage()
    r = await d._human_handoff(page, ATS["lever"], "lever:acme:1")
    assert r.status == "needs_human" and "headless=false" in r.detail and not d._watchers and page.clicks == 0


async def test_watcher_records_success_after_the_human_click():
    d, page = _driver(), FakePage()
    got: list[SubmitResult] = []

    async def cb(res):
        got.append(res)
    d.on_human_confirmed = cb
    await d._human_handoff(page, ATS["ashby"], "ashby:acme:1")
    await asyncio.sleep(0.2)
    assert not got                                         # nothing until the page confirms
    page.confirm()                                         # the person clicked; the ATS shows its success page
    res = await asyncio.wait_for(d.wait_human_click("ashby:acme:1"), 3)
    assert res is not None and res.status == "submitted" and got and got[0].status == "submitted"
    assert "confirmed-after-human-click" in d.stages and page.clicks == 0


@pytest.mark.parametrize("closed", [False, True])
async def test_watcher_timeout_or_closed_tab_leaves_handoff(closed):
    d, page = _driver(timeout=0.4), FakePage()
    got = []

    async def cb(res):
        got.append(res)
    d.on_human_confirmed = cb
    await d._human_handoff(page, ATS["lever"], "lever:acme:2")
    page.closed = closed
    assert await asyncio.wait_for(d.wait_human_click("lever:acme:2"), 3) is None
    assert not got and page.clicks == 0


async def test_engine_records_confirmed_human_click_through_state_machine():
    store = FakeStore()
    e = ApplyEngine(settings(), resolve_adapter=lambda r: FakeAdapter(), profile=FakeProfile(), router=None,
                    gates=FakeGates(), store=store, driver_factory=lambda n: None)
    e.approvals.set_state(POSTING.ref, "unknown", detail="handoff: form filled and verified")
    await e._human_confirmed(SubmitResult(ref=POSTING.ref, status="submitted", via="browser",
                                          detail="confirmed after human click"))
    assert e.approvals.state(POSTING.ref)["state"] == "submitted"
    assert [r.status for r, *_ in store.records] == ["submitted"]


def test_untrusted_config_cannot_relax_human_back_to_agent(tmp_path, monkeypatch, caplog):
    user = '[browser]\nfinal_click = "human"\n'
    with caplog.at_level("WARNING"):
        s = _cfg(tmp_path, monkeypatch, user=user, proj='[browser]\nfinal_click = "agent"\n')
    assert s.browser.final_click == "human" and "IGNORED" in caplog.text
    s = _cfg(tmp_path, monkeypatch, user=user, env={"JOB_APPLY__BROWSER__FINAL_CLICK": "agent"})
    assert s.browser.final_click == "human"
    monkeypatch.delenv("JOB_APPLY__BROWSER__FINAL_CLICK")
    s = _cfg(tmp_path, monkeypatch, proj='[browser]\nfinal_click = "human"\n')     # tightening: any layer
    assert s.browser.final_click == "human"


class _SeqEngine:
    """Fake engine for run_applications: records concurrency; every submit is a handoff confirmed later."""

    def __init__(self):
        self.active = self.max_active = 0
        self.order: list[str] = []
        self.approvals = types.SimpleNamespace(state=lambda ref: None)

    async def submit(self, ref, answers, approval_id, dry):
        self.active += 1
        self.max_active = max(self.max_active, self.active)
        self.order.append(ref)
        return SubmitResult(ref=ref, status="handoff", detail="form filled and verified; click Submit in the open tab")

    async def await_human_click(self, ref):
        await asyncio.sleep(0.05)
        self.active -= 1
        return SubmitResult(ref=ref, status="submitted") if ref.endswith("1") else None


async def test_run_applications_live_human_mode_is_one_tab_at_a_time():
    r = IntakeRunner.__new__(IntakeRunner)
    s = Settings()
    s.browser.final_click, s.apply.auto_approve, s.apply.approval_mode = "human", True, "agent"
    s.intake.submit_parallel = 4
    r.s, r.engine = s, _SeqEngine()
    cands = [Cand(target=f"https://h{i}.example-ats.test/job/{i}", source="links") for i in (1, 2, 3)]
    await r._submit_all(cands, dry=False)
    assert r.engine.max_active == 1 and r.engine.order == [c.target for c in cands]
    assert [c.status for c in cands] == ["submitted", "needs_human", "needs_human"]
    assert cands[0].why == "confirmed after human click" and "human_click_timeout_s" in cands[1].why

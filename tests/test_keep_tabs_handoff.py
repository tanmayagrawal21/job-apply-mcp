"""Filled-but-unsubmitted tabs stay open; handoff reasons are categorised (ADR-0067). Fictional data, fakes only."""
import asyncio
import json
import types
from pathlib import Path

import pytest

from job_apply_mcp import server
from job_apply_mcp.apply.browser_playwright import PlaywrightDriver
from job_apply_mcp.config import Settings
from job_apply_mcp.core import handoff
from job_apply_mcp.core.models import ApplicationForm, SubmitResult
from job_apply_mcp.intake.runner import IntakeRunner
from job_apply_mcp.services import Services

URL = "https://jobs.ashbyhq.com/example-co/00000000-0000-0000-0000-000000000001"
REF = "ashby:example-co:00000000-0000-0000-0000-000000000001"


class FakePage:
    def __init__(self):
        self.url, self.closed, self.handlers = URL, 0, {}

    async def close(self):
        self.closed += 1
        for h in self.handlers.get("close", []):
            h(self)

    def on(self, event, fn):
        self.handlers.setdefault(event, []).append(fn)


def _driver(tmp_path, **b):
    s = Settings()
    s.paths.data_dir = str(tmp_path)
    s.browser.headless = False
    for k, v in b.items():
        setattr(s.browser, k, v)
    d = PlaywrightDriver(s)
    d.pages = []

    async def new_page():
        p = FakePage()
        d.pages.append(p)
        return p

    async def ctx():
        return types.SimpleNamespace(new_page=new_page)
    d._context = ctx
    d._attach_recorder = lambda page, ats: None
    return d


async def _go(d, status, filled=True, raise_exc=False):
    async def run(page, ats, url, form, answers, files, dry_run):
        if filled:
            d._filled.add(id(page))
        if raise_exc:
            raise RuntimeError("page crashed after filling")
        return SubmitResult(ref=REF, status=status, via="browser", detail="fictional")
    d._run = run
    return await d.fill_and_submit(URL, ApplicationForm(ref=REF, fields=[]), {}, {}, dry_run=status == "dry_run")


@pytest.mark.parametrize("status", ["needs_human", "incomplete", "handoff", "roadblock", "dry_run", "unknown"])
async def test_filled_unsubmitted_tab_stays_open_and_is_listed(tmp_path, status):
    d = _driver(tmp_path)
    await _go(d, status)
    page = d.pages[0]
    assert page.closed == 0 and d.kept_tabs() == [{"ref": REF, "status": status, "url": URL}]
    assert json.loads((tmp_path / "kept_tabs.json").read_text()) == d.kept_tabs()
    await page.close()                                   # the person closes it: forgotten
    assert d.kept_tabs() == [] and json.loads((tmp_path / "kept_tabs.json").read_text()) == []


async def test_error_after_filling_keeps_the_tab(tmp_path):
    d = _driver(tmp_path)
    r = await _go(d, "", raise_exc=True)
    assert r.status == "error" and d.pages[0].closed == 0 and d.kept_tabs()[0]["status"] == "error"


@pytest.mark.parametrize("status,filled", [("submitted", True), ("skipped", True), ("duplicate", True),
                                           ("needs_human", False), ("skipped", False)])
async def test_submitted_closed_or_never_filled_tabs_close(tmp_path, status, filled):
    d = _driver(tmp_path)
    await _go(d, status, filled=filled)
    assert d.pages[0].closed == 1 and d.kept_tabs() == [] and not d._filled


@pytest.mark.parametrize("opts", [{"keep_unsubmitted_open": False}, {"headless": True}])
async def test_option_off_or_headless_closes(tmp_path, opts):
    d = _driver(tmp_path, **opts)
    await _go(d, "needs_human")
    assert d.pages[0].closed == 1 and d.kept_tabs() == []


def test_default_is_on_and_documented():
    assert Settings().browser.keep_unsubmitted_open is True
    assert Settings().browser.linger_after_disconnect_s > 0
    doc = (Path(__file__).resolve().parents[1] / "docs" / "configuration.md").read_text()
    assert "keep_unsubmitted_open" in doc and "linger_after_disconnect_s" in doc


# ---- the browser outlives a client disconnect while tabs are kept -------------------------------------------

class _D:
    def __init__(self, n):
        self.tabs = [{"ref": f"r{i}"} for i in range(n)]
        self._watchers = {}

    def kept_tabs(self):
        return self.tabs


def _with_drivers(monkeypatch, *drivers):
    eng = types.SimpleNamespace(_drivers={f"d{i}": d for i, d in enumerate(drivers)})
    monkeypatch.setattr(server, "_svc", types.SimpleNamespace(_engine=eng,
                        settings=types.SimpleNamespace(browser=types.SimpleNamespace(linger_after_disconnect_s=5))))


async def test_linger_waits_until_the_person_closes_the_tabs(monkeypatch):
    d = _D(1)
    _with_drivers(monkeypatch, d)

    async def close_later():
        await asyncio.sleep(0.1)
        d.tabs.clear()
    t = asyncio.create_task(close_later())
    left = await asyncio.wait_for(server.linger_for_kept_tabs(poll_s=0.02), 3)
    await t
    assert left == 0


async def test_linger_stops_at_the_cap_and_is_off_without_tabs(monkeypatch):
    _with_drivers(monkeypatch, _D(2))
    assert await server.linger_for_kept_tabs(poll_s=0.01, cap_s=0.05) == 2
    _with_drivers(monkeypatch, _D(0))
    assert await server.linger_for_kept_tabs(poll_s=0.01, cap_s=5) == 0
    _with_drivers(monkeypatch, _D(3))
    assert await server.linger_for_kept_tabs(poll_s=0.01, cap_s=0) == 0      # 0 = off


async def test_linger_also_waits_for_a_final_click_watcher(monkeypatch):
    d = _D(0)
    d._watchers = {"r": asyncio.create_task(asyncio.sleep(0.1))}
    _with_drivers(monkeypatch, d)
    assert await asyncio.wait_for(server.linger_for_kept_tabs(poll_s=0.02), 3) == 0


# ---- handoff categories ---------------------------------------------------------------------------------

@pytest.mark.parametrize("status,detail,cat", [
    ("needs_human", "comprehension check on the page", "hard_limit"),
    ("needs_human", "attestation: 'I certify'", "hard_limit"),
    ("handoff", "form filled and verified; click Submit in the open tab", "hard_limit"),
    ("handoff", "a human must click Submit (captcha-protected)", "hard_limit"),
    ("roadblock", "login before filling", "hard_limit"),
    ("awaiting_code", "", "hard_limit"),
    ("needs_human", "unresolved required: ['q_visa']", "unknown_fact"),
    ("needs_input", "", "unknown_fact"),
    ("needs_human", "could not fill/verify: ['q_school']", "tool_gap"),
    ("incomplete", "required fields still empty: ['Phone']", "tool_gap"),
    ("needs_human", "form did not render", "tool_gap"),
    ("needs_human", "unsupported_site: no deterministic filler for this host", "tool_gap"),
    ("unknown", "no confirmation detected", "tool_gap"),
    ("submitted", "", None),
    ("skipped", "closed", None),
])
def test_categorize_outside_a_consent_window(status, detail, cat):
    assert handoff.categorize(status, detail) == cat


@pytest.mark.parametrize("status,detail,scope", [
    ("needs_human", "awaiting_approval: run `job-apply-mcp approve x`", "approve_submissions"),
    ("roadblock", "login before filling", "credentials"),
    ("awaiting_code", "verification code requested", "otp_unattended"),
    ("handoff", "a human must click Submit (captcha-protected)", "captcha_attempt"),
])
def test_consented_steps_are_tool_gaps_inside_the_window(monkeypatch, status, detail, scope):
    monkeypatch.setattr(handoff, "_consented", lambda s, sc: sc == scope)
    assert handoff.categorize(status, detail, settings=object()) == "tool_gap"


def test_hard_limits_stay_hard_inside_the_window(monkeypatch):
    monkeypatch.setattr(handoff, "_consented", lambda s, sc: True)
    for detail in ("visible challenge on the page", "attestation: 'I certify'", "comprehension check"):
        assert handoff.categorize("needs_human", detail, settings=object()) == "hard_limit"


def test_hints_never_push_a_tool_gap_to_the_user_in_window():
    assert "do not hand this to the user" in handoff.category_hint("tool_gap", True)
    assert "ask the user" in handoff.category_hint("unknown_fact", True)
    assert handoff.category_hint(None, True) == ""


def test_submit_row_carries_category(tmp_path):
    s = Settings()
    s.paths.data_dir = str(tmp_path)
    svc = Services(s, engine=types.SimpleNamespace())
    row = svc._submit_row(SubmitResult(ref="r", status="needs_human", detail="could not fill/verify: ['q']"))
    assert row["cat"] == "tool_gap" and row["cat_hint"].startswith("tool_gap")
    assert "cat" not in svc._submit_row(SubmitResult(ref="r", status="submitted"))


def test_run_report_counts_tool_gaps():
    rows = [{"ref": f"r{i}", "source": "links", "status": st, "why": why} for i, (st, why) in enumerate([
        ("needs_human", "needs_human: could not fill/verify: ['q']"),
        ("needs_human", "incomplete: required fields still empty"),
        ("needs_human", "needs_human: unresolved required: ['q_visa']"),
        ("needs_human", "needs_human: attestation: 'I certify'"),
        ("submitted", ""),
    ])]
    rep = IntakeRunner._build("run-1", rows, 10)
    assert rep["handoff_categories"] == {"tool_gap": 2, "unknown_fact": 1, "hard_limit": 1}
    assert [r["cat"] for r in rep["needs_human"]] == ["tool_gap", "tool_gap", "unknown_fact", "hard_limit"]


# ---- next_action respects the category (ADR-0059 x ADR-0067) -------------------------------------------------

def test_tool_gap_in_window_never_asks_the_user(tmp_path, monkeypatch):
    from job_apply_mcp.core import next_steps
    monkeypatch.setattr(handoff, "_consented", lambda s, sc: True)
    s = Settings()
    s.paths.data_dir = str(tmp_path)
    svc = Services(s, engine=types.SimpleNamespace())
    row = svc._submit_row(SubmitResult(ref="r", status="needs_human", detail="could not fill/verify: ['q']"))
    assert row["cat"] == "tool_gap" and row["next_action"] == "retry_via_tool"
    assert row["next_action"] not in next_steps.HUMAN_ACTIONS
    row = svc._submit_row(SubmitResult(ref="r", status="unknown", detail="no confirmation detected"))
    assert row["next_action"] == "reconcile" and "never resubmit" in row["hint"]
    row = svc._submit_row(SubmitResult(ref="r", status="needs_human", detail="attestation: 'I certify'"))
    assert row["cat"] == "hard_limit" and row["next_action"] == "ask_user"      # hard limits still go to the user


def test_tool_gap_outside_window_keeps_the_normal_guidance(tmp_path):
    s = Settings()
    s.paths.data_dir = str(tmp_path)
    svc = Services(s, engine=types.SimpleNamespace())
    row = svc._submit_row(SubmitResult(ref="r", status="needs_human", detail="could not fill/verify: ['q']"))
    assert row["cat"] == "tool_gap" and row["next_action"] == "ask_user"


def test_unknown_state_guidance_comes_from_the_state(tmp_path):
    s = Settings()
    s.paths.data_dir = str(tmp_path)
    svc = Services(s, engine=types.SimpleNamespace())
    row = svc._submit_row(SubmitResult(ref="r", status="error", detail="TimeoutError: page closed"),
                          {"state": "unknown"})
    assert row["next_action"] == "verify_with_user" and "never resubmit" in row["hint"]


def test_degraded_hint_depends_on_dispatch():
    from job_apply_mcp.apply.ports import ComponentMissing
    from job_apply_mcp.core import output
    e = ComponentMissing("reviewer missing")
    assert "nothing was submitted" in output.degraded(e)["hint"]
    e.dispatched = True
    d = output.degraded(e)
    assert "never resubmit" in d["hint"] and "nothing was submitted" not in d["hint"]
    assert d["next_action"] == "verify_with_user"

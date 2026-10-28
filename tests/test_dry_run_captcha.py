"""Dry-run results surface captcha widgets (ADR-0071, issue #111). Result shaping only; fictional data, fakes only."""
import sqlite3
import types

import pytest

from job_apply_mcp.config import Settings
from job_apply_mcp.core import captcha_report
from job_apply_mcp.core.models import SubmitResult
from job_apply_mcp.core.next_steps import GUIDE, with_guide
from job_apply_mcp.intake.runner import Cand, IntakeRunner
from job_apply_mcp.intake.store import IntakeStore
from job_apply_mcp.services import Services


def st(widgets=(), visible=(), unsafe=()):
    return {"widgets": list(widgets), "visible": list(visible), "unsafe": list(unsafe)}


@pytest.mark.parametrize("state, ats, mode", [
    (st(), "greenhouse", "none"),
    (st(["recaptcha"]), "greenhouse", "implicit"),
    (st(["recaptcha", "hcaptcha"]), "lever", "implicit"),
    (st(["recaptcha"], ["recaptcha"]), "greenhouse", "challenge"),
    (st(["frame"], ["frame:https://example.test/challenge"]), "lever", "challenge"),
    (st(["hcaptcha"], unsafe=["hcaptcha-not-declared-invisible"]), "lever", "unclear"),
    (st(unsafe=["cdp-unavailable"]), "greenhouse", "unclear"),
    (st(), "ashby", "unclear"),                 # Ashby submits are always captcha-protected (ADR-0012)
    (st(["recaptcha"]), "ashby", "implicit"),
    (None, "greenhouse", "unclear"),
])
def test_summarize_modes(state, ats, mode):
    assert captcha_report.summarize(state, ats=ats)[1] == mode


def test_summarize_items_shape():
    items, mode = captcha_report.summarize(st(["recaptcha", "hcaptcha"], ["hcaptcha"]), enterprise=True)
    assert mode == "challenge"
    assert items == [{"kind": "recaptcha", "visible": False, "enterprise": True},
                     {"kind": "hcaptcha", "visible": True, "enterprise": None}]
    items, _ = captcha_report.summarize(st(["recaptcha"], unsafe=["frame-uninspectable"]))
    assert items == [{"kind": "recaptcha", "visible": None, "enterprise": None}]   # inconclusive -> null
    items, _ = captcha_report.summarize(st(unsafe=["unknown-vendor"]))
    assert items == [{"kind": "unknown", "visible": None, "enterprise": None}]
    items, _ = captcha_report.summarize(st(), ats="ashby")
    assert items == [{"kind": "ats:ashby", "visible": None, "enterprise": None}]
    assert captcha_report.summarize(st())[0] == []


def test_counts_ignores_uninspected_rows():
    rows = [{"captcha_mode": "none"}, {"captcha_mode": "implicit"}, {"captcha_mode": "none"}, {"status": "x"},
            "not-a-row", {"captcha_mode": "bogus"}]
    assert captcha_report.counts(rows) == {"none": 2, "implicit": 1}


@pytest.mark.parametrize("mode", ["implicit", "challenge", "unclear"])
def test_dry_run_hint_is_honest(mode):
    g = with_guide({"status": "dry_run", "captcha_mode": mode})
    assert g["next_action"] == "show_user"
    assert g["hint"] == captcha_report.HINTS[mode] and "nothing was sent" in g["hint"]
    assert len(g["hint"]) <= 200


def test_dry_run_hint_wording():
    assert "captcha_policy='attempt'" in captcha_report.HINTS["implicit"]
    assert "user must finish" in captcha_report.HINTS["challenge"]
    for h in captcha_report.HINTS.values():         # never suggests evasion or solving
        assert not any(w in h.lower() for w in ("solve", "bypass", "evade", "stealth"))


@pytest.mark.parametrize("row", [{"status": "dry_run"}, {"status": "dry_run", "captcha_mode": "none"}])
def test_dry_run_without_captcha_keeps_default_hint(row):
    assert with_guide(row)["hint"] == GUIDE["dry_run"][1]


def test_captcha_mode_only_changes_dry_run_hints():
    g = with_guide({"status": "handoff", "captcha_mode": "challenge"})
    assert g["hint"] == GUIDE["handoff"][1]


def _svc(tmp_path, engine=None):
    s = Settings()
    s.paths.data_dir = str(tmp_path)
    return Services(s, engine=engine or types.SimpleNamespace())


def test_submit_row_carries_captcha(tmp_path):
    items = [{"kind": "recaptcha", "visible": False, "enterprise": True}]
    row = _svc(tmp_path)._submit_row(SubmitResult(ref="r", status="dry_run", via="browser", captcha=items,
                                                  captcha_mode="implicit", detail="filled and verified 3 fields"))
    assert row["captcha"] == items and row["captcha_mode"] == "implicit"
    assert row["hint"] == captcha_report.HINTS["implicit"]
    none = _svc(tmp_path)._submit_row(SubmitResult(ref="r", status="dry_run", via="browser", captcha=[],
                                                   captcha_mode="none"))
    assert none["captcha_mode"] == "none" and "captcha" not in none
    api = _svc(tmp_path)._submit_row(SubmitResult(ref="r", status="dry_run", via="api"))
    assert "captcha_mode" not in api and "captcha" not in api      # not inspected: absent, never "none"


async def test_apply_batch_counts_captcha_modes(tmp_path):
    class Eng:
        approvals = types.SimpleNamespace(state=lambda ref: None)

        async def apply_batch(self, refs, dry, mp):
            return [{"ref": "a", "status": "dry_run", "captcha_mode": "none"},
                    {"ref": "b", "status": "dry_run", "captcha_mode": "implicit",
                     "captcha": [{"kind": "recaptcha", "visible": False, "enterprise": None}]},
                    {"ref": "c", "status": "dry_run", "captcha_mode": "challenge"},
                    {"ref": "d", "status": "skipped"}]

    out = await _svc(tmp_path, Eng()).apply_batch(["a", "b", "c", "d"], True, None)
    assert out["captcha_counts"] == {"none": 1, "implicit": 1, "challenge": 1}
    res = {r["ref"]: r for r in out["results"]}
    assert res["b"]["hint"] == captcha_report.HINTS["implicit"] and res["a"]["hint"] == GUIDE["dry_run"][1]
    assert [r["ref"] for r in out["results"] if r.get("captcha_mode") in ("none", "implicit")] == ["a", "b"]


def test_run_report_rows_and_counts():
    rows = [{"ref": "r1", "source": "links", "status": "dry_run_ok", "captcha": "none"},
            {"ref": "r2", "source": "links", "status": "dry_run_ok", "captcha": "implicit"},
            {"ref": "r3", "source": "links", "status": "dry_run_ok", "captcha": None},
            {"ref": "r4", "source": "links", "status": "skipped", "why": "policy"}]
    rep = IntakeRunner._build("run-1", rows, 10)
    assert rep["captcha_counts"] == {"none": 1, "implicit": 1}
    assert [r.get("captcha") for r in rep["dry_run_ok"]] == ["none", "implicit", None]
    assert "captcha" not in rep["skipped"][0]


def test_cand_row_carries_captcha():
    c = Cand("greenhouse:example:1", "links", status="dry_run_ok", captcha="implicit")
    assert c.row()["captcha"] == "implicit"


async def test_store_round_trips_and_migrates_captcha(tmp_path):
    db = sqlite3.connect(tmp_path / "applications.db")     # an intake_items table from before ADR-0071
    db.executescript("CREATE TABLE intake_items(id INTEGER PRIMARY KEY AUTOINCREMENT, run_id TEXT NOT NULL, "
                     "ref TEXT NOT NULL, url TEXT, source TEXT NOT NULL, status TEXT NOT NULL, why TEXT, "
                     "company TEXT, title TEXT, approval_id TEXT);")
    db.commit()
    db.close()
    store = IntakeStore(tmp_path)
    try:
        await store.add_items("run-1", [{"ref": "r1", "source": "links", "status": "dry_run_ok", "captcha": "none"},
                                        {"ref": "r2", "source": "links", "status": "skipped"}])
        got = await store.items("run-1")
        assert [i["captcha"] for i in got] == ["none", None]
    finally:
        store.close()


class _Page:
    def __init__(self, srcs=None, err=False):
        self.srcs, self.err = srcs or [], err

    async def evaluate(self, js):
        if self.err:
            raise RuntimeError("detached")
        return self.srcs


@pytest.mark.parametrize("page, want", [
    (_Page(["https://www.google.com/recaptcha/enterprise.js?render=k"]), True),
    (_Page(["https://www.google.com/recaptcha/api.js"]), False),
    (_Page([]), None),
    (_Page(err=True), None),
])
async def test_recaptcha_enterprise_report(page, want):
    from job_apply_mcp.apply.browser_playwright import PlaywrightDriver
    drv = object.__new__(PlaywrightDriver)
    assert await drv._recaptcha_enterprise(page) is want


@pytest.mark.parametrize("mode", ["none", "implicit", "challenge", "unclear"])
def test_dry_run_hint_final_click_human(mode):
    g = with_guide({"status": "dry_run", "captcha_mode": mode}, final_click="human")
    assert "final_click='human'" in g["hint"] and "user" in g["hint"] and "nothing was sent" in g["hint"]
    assert ("visible captcha" in g["hint"]) is (mode == "challenge") and len(g["hint"]) <= 200
    assert g["next_action"] == "show_user"


@pytest.mark.parametrize("mode", ["implicit", "challenge", "unclear"])
def test_dry_run_hint_captcha_handoff_off(mode):
    g = with_guide({"status": "dry_run", "captcha_mode": mode}, captcha_handoff=False)
    assert "captcha_handoff=false" in g["hint"] and "may reject" in g["hint"] and len(g["hint"]) <= 200
    assert with_guide({"status": "dry_run", "captcha_mode": "none"}, captcha_handoff=False)["hint"] \
        == GUIDE["dry_run"][1]
    # final_click=human wins: the user clicks whatever captcha_handoff says
    assert "final_click='human'" in with_guide({"status": "dry_run", "captcha_mode": mode}, final_click="human",
                                               captcha_handoff=False)["hint"]


def test_hint_wording_never_suggests_evasion():
    for mode in captcha_report.MODES:
        for fc in ("agent", "human"):
            for ch in (True, False):
                h = (captcha_report.hint(mode, final_click=fc, captcha_handoff=ch) or "").lower()
                assert not any(w in h for w in ("solve", "bypass", "evade", "stealth"))
    assert captcha_report.hint(None) is None and captcha_report.hint("bogus", final_click="human") is None


def test_submit_row_hint_follows_settings(tmp_path):
    svc = _svc(tmp_path)
    r = SubmitResult(ref="r", status="dry_run", via="browser", captcha=[], captcha_mode="implicit")
    svc.settings.browser.final_click = "human"
    assert "final_click='human'" in svc._submit_row(r)["hint"]
    svc.settings.browser.final_click = "agent"
    svc.settings.browser.captcha_handoff = False
    assert "captcha_handoff=false" in svc._submit_row(r)["hint"]


async def test_per_call_final_click_reaches_the_hint(tmp_path):
    class Eng:
        approvals = types.SimpleNamespace(state=lambda ref: None)
        timings = types.MappingProxyType({})

        async def submit(self, ref, answers, approval_id, dry_run, approver):
            return SubmitResult(ref=ref, status="dry_run", via="browser", captcha=[], captcha_mode="none")

    row = await _svc(tmp_path, Eng()).submit("r", {}, True, final_click="human")
    assert "final_click='human'" in row["hint"]
    row = await _svc(tmp_path, Eng()).submit("r", {}, True)
    assert row["hint"] == GUIDE["dry_run"][1]

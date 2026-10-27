"""Any MCP client can drive the tool end to end (ADR-0059, ADR-0060, ADR-0061). Fictional data and fakes only."""
import errno
import inspect
import json
import types
from pathlib import Path

import pytest
from apply_fakes import FORM, POSTING, F, FakeAdapter, FakeDriver, FakeGates, FakeProfile, FakeStore, settings

from job_apply_mcp import server
from job_apply_mcp.apply import ports
from job_apply_mcp.apply.engine import ApplyEngine, _Ctx
from job_apply_mcp.apply.ports import FINAL_CLICK, ComponentMissing, final_click
from job_apply_mcp.config import Settings
from job_apply_mcp.core import next_steps, output
from job_apply_mcp.core.models import ApplicationForm, FieldKind, SubmitResult
from job_apply_mcp.resume_access import documents_dir, readable, resolve_resume
from job_apply_mcp.services import Services

# ---- 1. a missing optional component degrades to needs_human, never a crash (ADR-0059) ---------------------------


async def test_component_missing_in_a_tool_is_needs_human_with_why_and_next_step(monkeypatch):
    class S:
        settings = types.SimpleNamespace(response_budget_chars=4000)

        async def draft(self, ref, cursor=0):
            raise ComponentMissing("router returned no text (client must write)")   # the writing reviewer's error

    monkeypatch.setattr(server, "_svc", S())
    d = json.loads(await server.draft_answers("ashby:example-co:new-grad-swe"))
    assert d["status"] == "needs_human" and d["ok"] is False
    assert "router returned no text" in d["why"] and d["next_action"] == "fix_setup" and d["hint"]
    assert d["error"]["code"] == "component_missing"


async def test_every_tool_degrades_component_missing(monkeypatch):
    class Boom:
        settings = types.SimpleNamespace(response_budget_chars=4000)

        def __getattr__(self, name):
            async def f(*a, **k):
                raise ComponentMissing(f"{name}: local model not installed")
            return f

    monkeypatch.setattr(server, "_svc", Boom())
    seen = 0
    for name in ("search_jobs", "get_job", "get_form", "plan_application", "draft_answers", "submit_application",
                 "apply_batch", "list_applications", "health"):
        fn = server.mcp._tool_manager._tools[name].fn
        params = inspect.signature(fn).parameters
        args: dict = {"ref_or_url": "x"} if "ref_or_url" in params else {}
        if name == "apply_batch":
            args = {"refs": ["x"]}
        if "ctx" in params:
            args["ctx"] = types.SimpleNamespace(report_progress=_noop, elicit=_noop)
        d = json.loads(await fn(**args))
        assert d["status"] == "needs_human" and d["next_action"] == "fix_setup", (name, d)
        seen += 1
    assert seen == 9


async def _noop(*a, **k):
    return None


def test_error_from_exc_keeps_other_errors_as_errors():
    d = json.loads(output.error_from_exc(RuntimeError("boom")))
    assert d["ok"] is False and d["error"]["code"] == "internal" and "status" not in d


async def test_batch_row_with_component_missing_is_needs_human():
    e = ApplyEngine(settings(), resolve_adapter=lambda r: FakeAdapter(), profile=FakeProfile(), router=None,
                    gates=FakeGates(), store=FakeStore(), driver_factory=lambda n: None)

    async def plan(ref):
        raise ComponentMissing("reviewer model not available")
    e.plan = plan
    rows = await e.apply_batch(["greenhouse:example-co:1"], dry_run=True)
    assert rows == [{"ref": "greenhouse:example-co:1", "status": "needs_human",
                     "detail": "component missing: reviewer model not available"}]


# ---- 2. full labels and options reach the client (ADR-0061) ---------------------------------------------------

LONG = ("Example Co builds tools for fictional robots. " * 8).strip() + " Are you authorized to work here?"


def test_full_label_keeps_long_labels_and_marks_only_past_the_cap():
    assert output.full_label(LONG) == LONG and len(LONG) > 300
    huge = "word " * 300
    cut = output.full_label(huge)
    assert cut.endswith("…(truncated)") and len(cut) == output.LABEL_MAX + len("…(truncated)")


def test_shaping_never_cuts_a_label_below_the_cap():
    tasks = [{"key": f"k{i}", "label": LONG, "options": [f"Option {j}" for j in range(25)],
              "context": {"posting": "z" * 3000}} for i in range(3)]
    d = json.loads(output.out({"status": "needs_input", "client_tasks": tasks}, 6000, "submit_application"))
    got = [t for t in d["client_tasks"] if "label" in t]
    assert got and all(t["label"] == LONG for t in got)
    assert all(len(t["options"]) == 25 for t in got if "options" in t)


async def test_get_form_and_client_tasks_return_full_labels_and_options():
    opts = [f"Choice {i}" for i in range(40)]
    form = ApplicationForm(ref=POSTING.ref, submit_via=["browser"], fields=[
        *FORM.fields, F("q_long", LONG, FieldKind.select, options=opts)])
    svc = Services(Settings())
    svc.resolve = lambda r: FakeAdapter(form=form)
    d = await svc.get_form(POSTING.ref)
    row = next(r for r in d["fields"] if r["k"] == "q_long")
    assert row["l"] == LONG and row["o"] == opts
    e = ApplyEngine(settings(), resolve_adapter=lambda r: FakeAdapter(form=form), profile=FakeProfile(), router=None,
                    gates=FakeGates(), store=FakeStore(), driver_factory=lambda n: None)
    out = await e.plan(POSTING.ref)
    t = next(t for t in out.client_tasks if t["key"] == "q_long")
    assert t["label"] == LONG and t["options"] == opts


# ---- 3. per-call fill-only mode, tighten-only (ADR-0060) ------------------------------------------------------

def test_final_click_is_tighten_only():
    assert final_click("agent") == "agent" and final_click("human") == "human"
    tok = FINAL_CLICK.set("human")
    try:
        assert final_click("agent") == "human"
    finally:
        FINAL_CLICK.reset(tok)
    tok = FINAL_CLICK.set("agent")          # never stored by the service, and harmless if it were
    try:
        assert final_click("human") == "human"
    finally:
        FINAL_CLICK.reset(tok)


@pytest.mark.parametrize("asked,expect", [("human", "human"), ("agent", None), (None, None), ("", None)])
async def test_service_sets_human_only_for_the_call(asked, expect):
    seen = []

    class Eng:
        approvals = types.SimpleNamespace(state=lambda ref: None)
        timings = types.MappingProxyType({})

        async def submit(self, *a):
            seen.append(ports.FINAL_CLICK.get())
            return SubmitResult(ref="r", status="handoff", detail="form filled and verified")

    svc = Services(Settings(), engine=Eng())
    row = await svc.submit("r", {}, None, final_click=asked)
    assert seen == [expect] and FINAL_CLICK.get() is None
    assert row["next_action"] == "user_clicks_submit"


async def test_service_rejects_unknown_final_click():
    svc = Services(Settings(), engine=types.SimpleNamespace())
    with pytest.raises(ValueError):
        await svc.submit("r", {}, None, final_click="robot")


class _HumanDriver(FakeDriver):
    on_human_confirmed = None

    async def fill_and_submit(self, apply_url, form, answers, files, *, dry_run=True):
        self.calls.append(dry_run)
        mode = final_click("agent")
        return SubmitResult(ref=form.ref, status="handoff" if mode == "human" else "submitted", via="browser")


async def _dispatch(driver_names, drivers, human):
    s = settings()
    s.browser.driver, s.browser.fallback_driver = driver_names
    ad = FakeAdapter()
    e = ApplyEngine(s, resolve_adapter=lambda r: ad, profile=FakeProfile(), router=None, gates=FakeGates(),
                    store=FakeStore(), driver_factory=lambda n: drivers.get(n))
    ctx = _Ctx(ad, POSTING, FORM, "")
    tok = FINAL_CLICK.set("human" if human else None)
    try:
        return await e._dispatch(ctx, {"first_name": "Ada"}, dry=False), ad
    finally:
        FINAL_CLICK.reset(tok)


async def test_human_call_skips_the_api_path_and_hands_off_in_the_browser():
    hd = _HumanDriver()
    res, ad = await _dispatch(("playwright", ""), {"playwright": hd}, human=True)
    assert res.status == "handoff" and ad.submits == [] and hd.calls == [False]
    res, ad = await _dispatch(("playwright", ""), {"playwright": _HumanDriver()}, human=False)
    assert res.status == "submitted" and ad.submits           # unchanged without the request


async def test_human_call_never_uses_a_driver_that_cannot_hand_off():
    agent_like = FakeDriver()                                    # no on_human_confirmed: it would click
    res, ad = await _dispatch(("browser_use", ""), {"browser_use": agent_like}, human=True)
    assert res.status == "needs_human" and agent_like.calls == [] and ad.submits == []


def test_playwright_driver_reads_the_per_call_mode():
    from job_apply_mcp.apply import browser_playwright
    assert 'final_click(self.b.final_click) == "human"' in inspect.getsource(browser_playwright)


# ---- 4. resume readable by this process (ADR-0061) ------------------------------------------------------------

def _s(tmp_path, resume=None):
    s = Settings()
    s.paths.data_dir, s.paths.resume = str(tmp_path / "data"), resume
    return s


def test_readable_resume_is_used(tmp_path):
    f = tmp_path / "cv.pdf"
    f.write_bytes(b"%PDF-1.4 fictional")
    acc = resolve_resume(_s(tmp_path, str(f)))
    assert acc.ok and acc.path == f and not acc.why


def test_blocked_resume_uses_only_an_identical_copy_in_documents(tmp_path, monkeypatch):
    blocked = tmp_path / "Downloads" / "cv.pdf"
    blocked.parent.mkdir()
    blocked.write_bytes(b"%PDF fictional v2")
    s = _s(tmp_path, str(blocked))
    assert resolve_resume(s).ok                          # readable once: its hash is recorded
    real_open = Path.open

    def fake_open(self, *a, **k):
        if self == blocked:
            raise PermissionError(errno.EPERM, "Operation not permitted")
        return real_open(self, *a, **k)
    monkeypatch.setattr(Path, "open", fake_open)
    acc = resolve_resume(s)
    assert not acc.ok and acc.path is None and "permission denied" in acc.why
    assert str(documents_dir(s)) in acc.fix
    docs = documents_dir(s)
    (docs / "cv.pdf").write_bytes(b"%PDF fictional v1 (older)")      # an older same-named copy: never used
    acc = resolve_resume(s)
    assert not acc.ok and "cannot be verified" in acc.why and str(docs / "cv.pdf") in acc.fix
    (docs / "cv.pdf").write_bytes(b"%PDF fictional v2")              # identical copy: used
    acc = resolve_resume(s)
    assert acc.ok and acc.path == docs / "cv.pdf" and "hash matches" in acc.why


def test_missing_configured_resume_never_falls_back(tmp_path):
    s = _s(tmp_path, str(tmp_path / "gone" / "cv.pdf"))
    docs = documents_dir(s)
    docs.mkdir(parents=True)
    (docs / "cv.pdf").write_bytes(b"%PDF fictional old")
    acc = resolve_resume(s)
    assert not acc.ok and acc.path is None and "not found" in acc.why


def test_relative_and_unset_resume_resolve_in_the_data_dir(tmp_path):
    s = _s(tmp_path, "resume.pdf")
    docs = documents_dir(s)
    docs.mkdir(parents=True)
    (docs / "resume.pdf").write_bytes(b"%PDF fictional")
    assert resolve_resume(s).path == docs / "resume.pdf"
    s.paths.resume = None
    acc = resolve_resume(s)
    assert acc.ok and acc.path == docs / "resume.pdf"
    (docs / "resume-old.pdf").write_bytes(b"%PDF fictional")
    assert not resolve_resume(s).ok                     # two candidates: ask, never pick one


def test_readable_reports_missing(tmp_path):
    assert readable(tmp_path / "nope.pdf") == (False, "not found")


async def test_health_reports_resume_access(tmp_path):
    svc = Services(_s(tmp_path, str(tmp_path / "missing.pdf")))

    class L:
        async def health(self):
            return {}
    svc._llm = L()
    h = await svc.health()
    assert h["resume"]["ok"] is False and "fix" in h["resume"] and h["final_click"] == "agent"


def test_doctor_flags_an_unreadable_resume(tmp_path):
    from job_apply_mcp import doctor
    s = _s(tmp_path, str(tmp_path / "missing.pdf"))
    s.paths.profile = str(Path(__file__).parent.parent / "examples" / "profile.example.toml")
    checks = {c.name: c for c in doctor._profile_checks(s)}
    assert checks["resume"].status == doctor.FAIL and "documents" in checks["resume"].fix


# ---- 5. next-step guidance on every status (ADR-0059) ---------------------------------------------------------

@pytest.mark.parametrize("st", ["planned", "needs_human", "incomplete", "handoff", "unknown", "roadblock",
                                "needs_input", "needs_review", "awaiting_code", "submitted", "dry_run", "error"])
def test_every_status_has_a_short_next_action_and_hint(st):
    g = next_steps.guide(st)
    assert g["next_action"].replace("_", "").isalpha() and 0 < len(g["hint"]) <= 110


def test_plan_status():
    assert next_steps.plan_status({"gate": "submit"}) == "planned"
    assert next_steps.plan_status({"gate": "submit", "unresolved": ["q"]}) == "needs_input"
    assert next_steps.plan_status({"gate": "submit", "client_tasks": [{"needs_human": True}]}) == "needs_human"
    assert next_steps.plan_status({"gate": "skip"}) == "skipped"


async def test_plan_and_submit_rows_carry_guidance():
    e = ApplyEngine(settings(), resolve_adapter=lambda r: FakeAdapter(), profile=FakeProfile(), router=None,
                    gates=FakeGates(), store=FakeStore(), driver_factory=lambda n: None)
    svc = Services(Settings(), engine=e)
    d = await svc.plan(POSTING.ref)
    assert d["status"] == "needs_input" and d["next_action"] == "ask_user"
    row = svc._submit_row(SubmitResult(ref="r", status="unknown"))
    assert row["next_action"] == "reconcile" and "never resubmit" in row["hint"]


# ---- 6. server instructions (ADR-0061) ------------------------------------------------------------------------

def test_server_instructions_tell_any_client_the_flow_and_the_rules():
    ins = server.mcp.instructions or ""
    # ADR-0064: the candidate-journal paragraph is appended as its own block after the main instructions
    assert ins == server.INSTRUCTIONS + "\n\n" + server._JOURNAL_INSTRUCTIONS
    assert len(server.INSTRUCTIONS) < 1200 and len(server._JOURNAL_INSTRUCTIONS) < 500
    for must in ("plan_application", "client_tasks", "submit_application", "final_click", "give_feedback",
                 "Never edit this tool's files or databases", "outside these tools", "next_action"):
        assert must in ins, must

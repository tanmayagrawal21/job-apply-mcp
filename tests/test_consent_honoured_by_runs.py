"""run_applications / apply_batch honour an owner-minted consent window for approve_submissions (ADR-0048).
The runner only reads the window; engine.submit meters it. Fictional data, fakes only, no network."""
import pytest
from apply_fakes import FORM
from test_apply_engine import _full_router, engine

from job_apply_mcp import consent
from job_apply_mcp.config import Settings
from job_apply_mcp.core.models import SubmitResult
from job_apply_mcp.intake.runner import Cand, IntakeRunner


class _Window:
    """A fake consent window with max uses; only the (mocked) engine.submit consumes it."""

    def __init__(self, uses):
        self.left, self.reads = uses, []

    def active(self, settings, scope):
        self.reads.append(scope)
        return object() if self.left > 0 else None

    def consume(self):
        if self.left <= 0:
            return False
        self.left -= 1
        return True


class _Engine:
    def __init__(self, window=None):
        self.calls, self.window = [], window
        self.approvals = type("A", (), {"state": staticmethod(lambda ref: None)})()

    async def submit(self, ref, answers, approval_id, dry):
        self.calls.append((ref, dry))
        if dry:
            return SubmitResult(ref=ref, status="dry_run")
        if self.window is not None and self.window.consume():
            return SubmitResult(ref=ref, status="submitted")
        return SubmitResult(ref=ref, status="needs_human", detail="awaiting human approval")


def _runner(eng):
    r = IntakeRunner.__new__(IntakeRunner)
    s = Settings()
    s.intake.submit_parallel = 1
    r.s, r.engine = s, eng
    return r


def _cands(n):
    return [Cand(target=f"https://jobs.example-ats.test/acme/{i}", source="links", approval_id=f"ap{i}")
            for i in range(n)]


async def test_runner_active_window_calls_submit(monkeypatch):
    w = _Window(5)
    monkeypatch.setattr(consent, "active", w.active)
    r, cands = _runner(_Engine(w)), _cands(2)
    await r._submit_all(cands, dry=False)
    assert [c for c, _ in r.engine.calls] == [c.target for c in cands]
    assert [c.status for c in cands] == ["submitted", "submitted"]
    assert set(w.reads) == {"approve_submissions"}


async def test_runner_no_window_is_awaiting_approval(monkeypatch):
    monkeypatch.setattr(consent, "active", lambda s, scope: None)
    r, cands = _runner(_Engine()), _cands(2)
    await r._submit_all(cands, dry=False)
    assert r.engine.calls == []
    assert all(c.status == "needs_human" and c.why.startswith("awaiting_approval") for c in cands)


async def test_runner_exhausted_window_rest_await_approval(monkeypatch):
    w = _Window(1)
    monkeypatch.setattr(consent, "active", w.active)
    r, cands = _runner(_Engine(w)), _cands(3)
    await r._submit_all(cands, dry=False)
    assert len(r.engine.calls) == 1 and cands[0].status == "submitted"
    assert all(c.status == "needs_human" and c.why.startswith("awaiting_approval") for c in cands[1:])


async def test_runner_dry_run_unchanged(monkeypatch):
    def boom(*a):
        raise AssertionError("a dry run must not consult consent")
    monkeypatch.setattr(consent, "active", boom)
    r, cands = _runner(_Engine()), _cands(2)
    await r._submit_all(cands, dry=True)
    assert r.engine.calls == [(c.target, True) for c in cands]


def test_live_preview_note_reflects_window(monkeypatch):
    r = _runner(_Engine())
    r.s.dry_run = False
    monkeypatch.setattr(consent, "active", lambda s, scope: None)
    assert "approval_mode needs a human" in " ".join(r._live_preview([]).get("warning", []))
    monkeypatch.setattr(consent, "active", lambda s, scope: object())
    assert "approval_mode needs a human" not in " ".join((r._live_preview([]) or {}).get("warning", []))


def _batch_engine(monkeypatch, window):
    e, ad = engine(router=_full_router(), writing__max_rounds=1)
    ad.form = FORM.model_copy(update={"fields": [f for f in FORM.fields if f.key != "q_why"]})
    fake = _Engine(window)
    monkeypatch.setattr(e, "submit", fake.submit)
    return e, fake


@pytest.mark.parametrize("uses,want", [(5, ["submitted", "submitted"]), (1, ["submitted", "awaiting_approval"]),
                                       (0, ["awaiting_approval", "awaiting_approval"])])
async def test_apply_batch_honours_window(monkeypatch, uses, want):
    w = _Window(uses)
    monkeypatch.setattr(consent, "active", w.active)
    e, fake = _batch_engine(monkeypatch, w)
    refs = ["greenhouse:acme:1", "greenhouse:acme:2"]
    rows = []
    for ref in refs:                      # one at a time so the exhaustion order is deterministic
        rows += await e.apply_batch([ref], dry_run=False)
    assert [r["status"] for r in rows] == want
    assert len(fake.calls) == want.count("submitted")


async def test_apply_batch_dry_run_unchanged(monkeypatch):
    monkeypatch.setattr(consent, "active", lambda s, scope: None)
    e, fake = _batch_engine(monkeypatch, None)
    rows = await e.apply_batch(["greenhouse:acme:1"], dry_run=True)
    assert rows[0]["status"] == "dry_run" and fake.calls == [("greenhouse:acme:1", True)]

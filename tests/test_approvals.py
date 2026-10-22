"""ADR-0021: server-held approvals, human mode, safe-only overrides, submission state machine."""
import hashlib
import json

import pytest
from apply_fakes import POSTING, FakeAdapter, FakeDriver, FakeGates, FakeProfile, FakeStore, settings
from test_apply_engine import _full_router

from job_apply_mcp.apply.approvals import ApprovalBook, cli
from job_apply_mcp.apply.engine import ApplyEngine
from job_apply_mcp.config import Settings, load_settings

EXTRA = {"q_why": "Because Acme Robotics builds warehouse fleets."}


def make(book=None, adapter=None, **cfg):
    ad = adapter or FakeAdapter()
    cfg.setdefault("writing__max_rounds", 1)
    e = ApplyEngine(settings(**cfg), resolve_adapter=lambda r: ad, profile=FakeProfile(), router=_full_router(),
                    gates=FakeGates(), store=FakeStore(), approvals=book, driver_factory=lambda n: FakeDriver())
    return e, ad


def _client_token(ref, answers):
    return hashlib.sha256(json.dumps({"ref": ref, "answers": answers}, sort_keys=True, separators=(",", ":"),
                                     default=str).encode()).hexdigest()


async def test_client_computed_sha256_token_is_rejected():
    e, ad = make()
    out = await e.plan(POSTING.ref)
    full = {**{a.key: a.value for a in out.plan.answers}, **EXTRA}
    for fake in (_client_token(POSTING.ref, full), _client_token(POSTING.ref, {}), "0" * 64):
        r = await e.submit(POSTING.ref, full, fake, dry_run=False)
        assert r.status == "error" and "approval_id" in r.detail
    assert not ad.submits
    e2, ad2 = make()                                          # fresh server: never saw this plan either
    r = await e2.submit(POSTING.ref, full, _client_token(POSTING.ref, full), dry_run=False)
    assert r.status == "error" and not ad2.submits


async def test_approval_id_is_opaque_random_and_bound_to_ref():
    e, _ = make()
    a, b = (await e.plan(POSTING.ref)).approval_id, (await e.plan(POSTING.ref)).approval_id
    assert a != b and len(a) >= 24 and not set(a) <= set("0123456789abcdef")
    r = await e.submit("greenhouse:other:9", EXTRA, a, dry_run=False)
    assert r.status == "error"


async def test_agent_mode_submits_with_server_id_and_audits():
    e, ad = make()
    out = await e.plan(POSTING.ref)
    r = await e.submit(POSTING.ref, EXTRA, out.approval_id, dry_run=False)
    assert r.status == "submitted" and ad.submits
    assert e.approvals.state(POSTING.ref)["state"] == "submitted"
    events = [(x["event"], x["actor"]) for x in e.approvals.audit_rows(POSTING.ref)]
    assert ("approved", "agent") in events and ("state:submitting", "server") in events
    r = await e.submit(POSTING.ref, EXTRA, out.approval_id, dry_run=False)
    assert r.status != "submitted" and len(ad.submits) == 1     # id is single-use and ref is done


async def test_human_mode_refuses_until_human_approves_cli():
    e, ad = make(apply__approval_mode="human")
    out = await e.plan(POSTING.ref)
    r = await e.submit(POSTING.ref, EXTRA, out.approval_id, dry_run=False)
    assert r.status == "needs_human" and "approve" in r.detail and not ad.submits
    ok, _ = e.approvals.approve(out.approval_id, "human:cli")      # what `job-apply-mcp approve <id>` does
    assert ok
    r = await e.submit(POSTING.ref, EXTRA, out.approval_id, dry_run=False)
    assert r.status == "submitted"
    assert ("approved", "human:cli") in [(x["event"], x["actor"]) for x in e.approvals.audit_rows()]


async def test_human_mode_elicitation_approve_and_deny():
    e, _ = make(apply__approval_mode="human")
    seen = []

    async def yes(summary):
        seen.append(summary)
        return True
    out = await e.plan(POSTING.ref)
    r = await e.submit(POSTING.ref, EXTRA, out.approval_id, dry_run=False, approver=yes)
    assert r.status == "submitted" and seen[0]["company"] == "Acme Robotics" and "q_why" in seen[0]["answers"]
    e2, ad2 = make(apply__approval_mode="human")
    out = await e2.plan(POSTING.ref)

    async def no(summary):
        return False
    r = await e2.submit(POSTING.ref, EXTRA, out.approval_id, dry_run=False, approver=no)
    assert r.status == "error" and "denied" in r.detail and not ad2.submits


async def test_any_answer_change_invalidates_human_approval():
    e, ad = make(apply__approval_mode="human")
    out = await e.plan(POSTING.ref)
    e.approvals.approve(out.approval_id, "human:cli")               # approved the planned answers (no q_why yet)
    r = await e.submit(POSTING.ref, EXTRA, out.approval_id, dry_run=False)   # client adds text -> hash changes
    assert r.status == "needs_human" and not ad.submits
    assert e.approvals.get(out.approval_id).status == "pending"
    e.approvals.approve(out.approval_id, "human:cli")
    r = await e.submit(POSTING.ref, {"q_why": "different"}, out.approval_id, dry_run=False)   # changed again
    assert r.status == "needs_human" and not ad.submits
    e.approvals.approve(out.approval_id, "human:cli")
    assert (await e.submit(POSTING.ref, {"q_why": "different"}, out.approval_id, dry_run=False)).status == "submitted"


async def test_file_change_invalidates(tmp_path):
    f = tmp_path / "cv.pdf"
    f.write_bytes(b"one")
    book = ApprovalBook()
    aid = book.issue("r", "u", "c", "t", {"a": 1}, {"resume": str(f)})
    rec = book.get(aid)
    assert book.matches(rec, {"a": 1}, {"resume": str(f)})
    f.write_bytes(b"two")
    assert not book.matches(rec, {"a": 1}, {"resume": str(f)})


async def test_expired_approval_rejected():
    now = [1000.0]
    book = ApprovalBook(ttl_s=10, clock=lambda: now[0])
    e, _ = make(book=book)
    out = await e.plan(POSTING.ref)
    now[0] += 11
    r = await e.submit(POSTING.ref, EXTRA, out.approval_id, dry_run=False)
    assert r.status == "error" and "expired" in r.detail


async def test_per_call_dry_run_false_refused_when_global_dry():
    e, ad = make(dry_run=True)
    out = await e.plan(POSTING.ref)
    r = await e.submit(POSTING.ref, EXTRA, out.approval_id, dry_run=False)
    assert r.status == "error" and "global dry_run" in r.detail and not ad.submits
    rows = await e.apply_batch([POSTING.ref], dry_run=False)
    assert rows[0]["status"] == "error"
    assert (await e.submit(POSTING.ref, EXTRA, None, dry_run=True)).status == "dry_run"
    assert (await e.submit(POSTING.ref, EXTRA, None)).status == "dry_run"      # None inherits global
    e2, _ = make(dry_run=False)                                                 # per-call may tighten
    assert (await e2.submit(POSTING.ref, EXTRA, None, dry_run=True)).status == "dry_run"


def test_auto_approve_only_from_user_config(tmp_path, monkeypatch, caplog):
    user = tmp_path / "user.toml"
    user.write_text('[apply]\napproval_mode = "agent"\nauto_approve = true\n')
    proj = tmp_path / "proj"
    proj.mkdir()
    monkeypatch.setenv("JOB_APPLY_MCP_CONFIG", str(user))
    with caplog.at_level("WARNING"):
        s = load_settings(proj)
    assert s.apply.auto_approve and s.apply.auto_approve_active and "auto_approve=true" in caplog.text
    user.write_text('[apply]\napprovalmode = 1\n')
    (proj / "job-apply-mcp.toml").write_text("[apply]\nauto_approve = true\n")
    monkeypatch.setenv("JOB_APPLY__APPLY__AUTO_APPROVE", "true")
    assert load_settings(proj).apply.auto_approve is False                 # project file + env ignored
    user.write_text('[apply]\nauto_approve = true\n')                      # default human mode -> ignored
    s = load_settings(tmp_path / "none")
    assert s.apply.auto_approve and not s.apply.auto_approve_active


def test_defaults_are_safe():
    s = Settings()
    assert s.dry_run and s.apply.approval_mode == "human" and not s.apply.auto_approve


# ---- submission state machine
class Boom(FakeAdapter):
    async def submit(self, form, answers, files, *, dry_run=True):
        # the durable pre-submit record must exist while the click is in flight
        self.state_during = self.book.state(form.ref)["state"]
        raise TimeoutError("network died after click")


async def test_submitting_written_before_click_and_failure_becomes_unknown(tmp_path):
    book = ApprovalBook(tmp_path)
    ad = Boom()
    ad.book = book
    e, _ = make(book=book, adapter=ad)
    out = await e.plan(POSTING.ref)
    r = await e.submit(POSTING.ref, EXTRA, out.approval_id, dry_run=False)
    assert r.status == "error" and ad.state_during == "submitting"
    assert book.state(POSTING.ref)["state"] == "unknown"


@pytest.mark.parametrize("state", ["submitting", "unknown"])
async def test_stuck_ref_never_auto_resubmitted_even_after_restart(tmp_path, state):
    book = ApprovalBook(tmp_path)
    e, ad = make(book=book)
    await e.plan(POSTING.ref)
    book.set_state(POSTING.ref, state, detail="crash")
    book2 = ApprovalBook(tmp_path)                                   # "restart": new process, same db
    e2, ad2 = make(book=book2)
    out2 = await e2.plan(POSTING.ref)
    r = await e2.submit(POSTING.ref, EXTRA, out2.approval_id, dry_run=False)
    assert r.status == "needs_human" and state in r.detail and "resolve_submission" in r.detail
    assert not ad2.submits and not ad.submits
    st = await e2.resolve_submission(POSTING.ref)                    # inspect only
    assert st["state"] == state and st["idem"]


async def test_resolve_submission_moves_on_and_records():
    e, _ = make()
    await e.plan(POSTING.ref)
    e.approvals.set_state(POSTING.ref, "unknown", detail="x")
    bad = await e.resolve_submission(POSTING.ref, "bogus")
    assert "error" in bad
    res = await e.resolve_submission(POSTING.ref, "submitted", "confirmed by email", actor="human")
    assert res["state"] == "submitted"
    assert e.store.records and e.store.records[0][0].status == "submitted"
    assert "error" in await e.resolve_submission(POSTING.ref, "failed")      # only submitting/unknown resolvable
    e2, _ = make()
    await e2.plan(POSTING.ref)
    e2.approvals.set_state(POSTING.ref, "submitting")
    assert (await e2.resolve_submission(POSTING.ref, "failed", "never sent"))["state"] == "failed"
    out2 = await e2.plan(POSTING.ref)                                        # failed -> may be retried
    assert (await e2.submit(POSTING.ref, EXTRA, out2.approval_id, dry_run=False)).status == "submitted"


async def test_idempotency_key_stable_per_ref_and_state_flow():
    e, _ = make()
    out = await e.plan(POSTING.ref)
    assert e.approvals.state(POSTING.ref)["state"] == "planned"
    k = e.approvals.state(POSTING.ref)["idem_key"]
    e.approvals.approve(out.approval_id, "human:cli")
    assert e.approvals.state(POSTING.ref)["state"] == "approved"
    await e.submit(POSTING.ref, EXTRA, out.approval_id, dry_run=False)
    st = e.approvals.state(POSTING.ref)
    assert st["state"] == "submitted" and st["idem_key"] == k


async def test_needs_human_result_becomes_handoff():
    e, _ = make(adapter=FakeAdapter(submit_status="needs_human"))
    out = await e.plan(POSTING.ref)
    await e.submit(POSTING.ref, EXTRA, out.approval_id, dry_run=False)
    assert e.approvals.state(POSTING.ref)["state"] == "handoff"


def test_cli_approve_deny_resolve(tmp_path, capsys):
    book = ApprovalBook(tmp_path)
    aid = book.issue("r1", "u", "Acme", "Eng", {"a": "1"})
    assert cli(["pending"], tmp_path) == 0 and aid in capsys.readouterr().out
    assert cli(["approve", aid], tmp_path) == 0
    assert ApprovalBook(tmp_path).get(aid).approved_by == "human:cli"
    assert cli(["approve", "nope"], tmp_path) == 1
    assert cli(["deny", aid], tmp_path) == 0 and ApprovalBook(tmp_path).get(aid).status == "denied"
    book.set_state("r1", "unknown")
    assert cli(["resolve", "r1", "failed", "checked"], tmp_path) == 0
    assert cli([ "bogus"], tmp_path) == 2


async def test_server_elicit_approver_handles_unsupported_client():
    from types import SimpleNamespace

    from job_apply_mcp.server import _elicit_approver

    class NoSupport:
        async def elicit(self, **kw):
            raise RuntimeError("client does not support elicitation")

    class Accept:
        async def elicit(self, message, schema):
            assert "Acme" in message
            return SimpleNamespace(action="accept", data=SimpleNamespace(approve=True))

    class Decline:
        async def elicit(self, message, schema):
            return SimpleNamespace(action="decline", data=None)
    s = {"company": "Acme", "title": "Eng", "answers": {"a": "1"}}
    assert await _elicit_approver(NoSupport())(s) is None
    assert await _elicit_approver(Accept())(s) is True
    assert await _elicit_approver(Decline())(s) is False

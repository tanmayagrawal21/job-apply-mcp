"""ADR-0021: server-held approvals, human mode, safe-only overrides, submission state machine."""
import hashlib
import json
import os

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
    assert "error" in await e.resolve_submission(POSTING.ref, "failed", actor="human")      # only submitting/unknown resolvable
    e2, _ = make()
    await e2.plan(POSTING.ref)
    e2.approvals.set_state(POSTING.ref, "submitting")
    assert (await e2.resolve_submission(POSTING.ref, "failed", "never sent", actor="human"))["state"] == "failed"
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



async def test_roadblock_result_becomes_unknown_never_retryable():
    """A captcha/verification wall can appear AFTER the click: the application may have been received."""
    e, _ = make(adapter=FakeAdapter(submit_status="roadblock"))
    out = await e.plan(POSTING.ref)
    await e.submit(POSTING.ref, EXTRA, out.approval_id, dry_run=False)
    assert e.approvals.state(POSTING.ref)["state"] == "unknown"

class _Tty:
    def __init__(self, tty):
        self.tty = tty

    def isatty(self):
        return self.tty


def _run(argv, tmp_path, answers=(), tty=True):
    it = iter(answers)
    return cli(argv, tmp_path, stdin=_Tty(tty), stdout=_Tty(tty), input_fn=lambda prompt: next(it))


def test_cli_refuses_without_tty_and_needs_typed_confirmation(tmp_path, capsys):
    book = ApprovalBook(tmp_path)
    aid = book.issue("r1", "u", "Acme", "Eng", {"a": "1"})
    assert _run(["pending"], tmp_path, tty=False) == 0 and aid in capsys.readouterr().out
    for cmd in (["approve", aid], ["deny", aid], ["resolve", "r1", "failed"]):
        assert _run(cmd, tmp_path, tty=False) == 3
    assert "refused" in capsys.readouterr().out and ApprovalBook(tmp_path).get(aid).status == "pending"
    assert _run(["approve", aid], tmp_path, ["wrong"]) == 1 and ApprovalBook(tmp_path).get(aid).status == "pending"
    assert _run(["approve", aid], tmp_path, ["acme"]) == 0                # full summary printed, typed company
    assert '"company": "Acme"' in capsys.readouterr().out
    assert ApprovalBook(tmp_path).get(aid).approved_by == "human:cli"
    assert _run(["approve", "nope"], tmp_path) == 1
    assert _run(["deny", aid], tmp_path, ["y"]) == 0 and ApprovalBook(tmp_path).get(aid).status == "denied"
    book.set_state("r1", "unknown")
    assert _run(["resolve", "r1", "failed", "checked"], tmp_path, ["nope"]) == 1
    assert _run(["resolve", "r1", "failed", "checked"], tmp_path, ["failed"]) == 0
    assert _run(["bogus"], tmp_path) == 2


def test_cli_dispatch_via_main_entry(tmp_path, monkeypatch):
    from job_apply_mcp import cli as entry
    monkeypatch.setenv("JOB_APPLY__PATHS__DATA_DIR", str(tmp_path))
    with pytest.raises(SystemExit) as ei:
        entry.main(["approve", "x"])        # pytest stdin is not a TTY
    assert ei.value.code == 3


async def test_agent_resolve_needs_agent_mode_and_evidence(tmp_path):
    e, _ = make(apply__approval_mode="human")
    await e.plan(POSTING.ref)
    e.approvals.set_state(POSTING.ref, "unknown")
    r = await e.resolve_submission(POSTING.ref, "failed", "x")                  # agent in human mode
    assert "human-only" in r["error"] and e.approvals.state(POSTING.ref)["state"] == "unknown"
    e2, _ = make(apply__approval_mode="agent", paths__data_dir=str(tmp_path))
    await e2.plan(POSTING.ref)
    e2.approvals.set_state(POSTING.ref, "unknown")
    assert "evidence" in (await e2.resolve_submission(POSTING.ref, "failed", "x"))["error"]
    ev = tmp_path / "evidence" / hashlib.sha256(POSTING.ref.encode()).hexdigest()[:12]
    ev.mkdir(parents=True)
    assert "error" in await e2.resolve_submission(POSTING.ref, "failed", "x", evidence="/tmp/elsewhere")
    r = await e2.resolve_submission(POSTING.ref, "failed", "x", evidence=str(ev))
    assert r["state"] == "failed"
    assert any(x["event"] == "resolved" and x["actor"] == "agent" for x in e2.approvals.audit_rows(POSTING.ref))


def test_resolve_tool_absent_in_human_mode_and_legacy_token_errors(monkeypatch):
    import asyncio

    from job_apply_mcp import server
    names = {t.name for t in asyncio.run(server.mcp.list_tools())}
    assert "resolve_submission" not in names                        # default mode is human
    out = json.loads(asyncio.run(server.submit_application(None, "greenhouse:acme:1", token="abc")))
    assert out["status"] == "error" and "no longer accepted" in out["detail"] and "approval_id" in out["detail"]


def _cfg(tmp_path, monkeypatch, user="", proj="", env=None, via_env=False, mode=0o600):
    """User config at the platform-default path (XDG redirected to tmp), or selected via JOB_APPLY_MCP_CONFIG."""
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "xdg"))
    monkeypatch.setenv("APPDATA", str(tmp_path / "xdg"))
    from job_apply_mcp import paths
    cdir = paths.config_dir()
    cdir.mkdir(parents=True, exist_ok=True)
    monkeypatch.delenv("JOB_APPLY_MCP_CONFIG", raising=False)
    u = tmp_path / "elsewhere.toml" if via_env else cdir / "config.toml"
    u.write_text(user)
    u.chmod(mode)
    if via_env:
        monkeypatch.setenv("JOB_APPLY_MCP_CONFIG", str(u))
    pd = tmp_path / "proj"
    pd.mkdir(exist_ok=True)
    (pd / "job-apply-mcp.toml").write_text(proj)
    for k, v in (env or {}).items():
        monkeypatch.setenv(k, v)
    return load_settings(pd)


def test_security_settings_relax_only_from_user_config(tmp_path, monkeypatch, caplog):
    relax = ('dry_run = false\n[apply]\napproval_mode = "agent"\nauto_approve = true\n'
             '[control]\napply_enabled = true\ndev_mode = false\n')
    strict_user = 'dry_run = true\n[control]\napply_enabled = false\ndev_mode = true\n[apply]\napproval_mode="human"\n'
    with caplog.at_level("WARNING"):
        s = _cfg(tmp_path, monkeypatch, user=strict_user, proj=relax)
    assert s.dry_run and s.apply.approval_mode == "human" and not s.apply.auto_approve
    assert not s.control.apply_enabled and s.control.dev_mode
    assert "job-apply-mcp.toml" in caplog.text and "IGNORED" in caplog.text
    env = {"JOB_APPLY__DRY_RUN": "false", "JOB_APPLY__APPLY__APPROVAL_MODE": "agent",
           "JOB_APPLY__CONTROL__DEV_MODE": "false", "JOB_APPLY__CONTROL__APPLY_ENABLED": "true"}
    s = _cfg(tmp_path, monkeypatch, user=strict_user, env=env)
    assert s.dry_run and s.apply.approval_mode == "human" and s.control.dev_mode and not s.control.apply_enabled
    s = _cfg(tmp_path, monkeypatch, user=relax)                      # user file may relax (and it is logged)
    assert not s.dry_run and s.apply.approval_mode == "agent" and s.apply.auto_approve
    s = _cfg(tmp_path, monkeypatch, user=relax, proj='dry_run = true\n[apply]\napproval_mode = "human"\n')
    assert s.dry_run and s.apply.approval_mode == "human"            # project/env may tighten


async def test_adapter_error_before_dispatch_is_failed_after_dispatch_unknown():
    class ErrAdapter(FakeAdapter):
        async def submit(self, form, answers, files, *, dry_run=True):
            self.submits.append(1)
            from job_apply_mcp.core.models import SubmitResult
            return SubmitResult(ref=form.ref, status="error", via="api", detail="HTTP 500")
    e, ad = make(adapter=ErrAdapter())
    out = await e.plan(POSTING.ref)
    r = await e.submit(POSTING.ref, EXTRA, out.approval_id, dry_run=False)
    assert r.status == "error" and ad.submits and e.approvals.state(POSTING.ref)["state"] == "unknown"
    # no driver and no api path: nothing was dispatched, so an error is retryable (failed), not unknown
    e2, _ = make()
    out = await e2.plan(POSTING.ref)

    async def pre(ctx, eff, dry):
        from job_apply_mcp.core.models import SubmitResult
        return SubmitResult(ref=ctx.form.ref, status="error", detail="could not start")
    e2._dispatch = pre
    r = await e2.submit(POSTING.ref, EXTRA, out.approval_id, dry_run=False)
    assert e2.approvals.state(POSTING.ref)["state"] == "failed"


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


async def test_error_before_dispatch_is_failed_not_unknown():
    e, _ = make()
    out = await e.plan(POSTING.ref)

    async def pre(ctx, eff, dry):          # raises before any adapter/driver call (dispatched flag unset)
        raise RuntimeError("failed before any click")
    e._dispatch = pre
    r = await e.submit(POSTING.ref, EXTRA, out.approval_id, dry_run=False)
    assert r.status == "error" and e.approvals.state(POSTING.ref)["state"] == "failed"


_RELAX = 'dry_run = false\n[apply]\napproval_mode = "agent"\nauto_approve = true\n[control]\ndev_mode = false\n'


@pytest.mark.skipif(os.name == "nt", reason="posix permission model")
def test_env_selected_config_cannot_relax(tmp_path, monkeypatch, caplog):
    with caplog.at_level("WARNING"):
        s = _cfg(tmp_path, monkeypatch, user=_RELAX, via_env=True)
    assert s.dry_run and s.apply.approval_mode == "human" and not s.apply.auto_approve
    assert "platform-default" in caplog.text
    s = _cfg(tmp_path, monkeypatch, user='dry_run = true\n[control]\ndev_mode = true\n', via_env=True)
    assert s.dry_run and s.control.dev_mode                          # stricter is still honored


@pytest.mark.skipif(os.name == "nt", reason="posix permission model")
@pytest.mark.parametrize("mode", [0o666, 0o662, 0o620])
def test_group_or_world_writable_default_config_cannot_relax(tmp_path, monkeypatch, mode, caplog):
    with caplog.at_level("WARNING"):
        s = _cfg(tmp_path, monkeypatch, user=_RELAX, mode=mode)
    assert s.dry_run and s.apply.approval_mode == "human" and "writable" in caplog.text
    s = _cfg(tmp_path, monkeypatch, user=_RELAX, mode=0o644)         # owner-only-writable default file is trusted
    assert not s.dry_run and s.apply.approval_mode == "agent"

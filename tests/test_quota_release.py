"""ADR-0032: a live submit that provably sent nothing gives its quota back (audited, replayed)."""
import json

import pytest
from apply_fakes import POSTING, FakeAdapter
from test_approvals import EXTRA, make
from test_control import mk

QUOTA = {"quota": {"per_day": 2, "per_run": 2, "per_platform_per_day": 2}}


async def _submit(a, ref):
    return await a.check("submit", {"ref": ref, "platform": "lever", "dry_run": False})


async def test_released_ref_not_counted_in_day_platform_run_quotas(tmp_path):
    a = mk(tmp_path, params=QUOTA)
    await _submit(a, "r1")
    await _submit(a, "r2")
    assert (await _submit(a, "r3")).decision == "block"
    await a.release("r1", "fill failed")
    assert a.status()["quotas"]["day"] == {"used": 1, "left": 1}
    assert a.status()["quotas"]["run"] == {"used": 1, "left": 1}
    assert (await _submit(a, "r3")).decision == "pass"   # day, run and platform caps all had room again
    assert (await _submit(a, "r4")).decision == "block"


async def test_released_ref_can_be_retried_by_single_submit(tmp_path):
    a = mk(tmp_path)
    await _submit(a, "r1")
    assert (await _submit(a, "r1")).decision == "block"
    await a.release("r1", "x")
    assert not a.state.seen_ref("r1", frozenset({"submit"}), 0)


async def test_release_survives_replay_and_is_audited(tmp_path):
    a = mk(tmp_path, params=QUOTA)
    await _submit(a, "r1")
    await _submit(a, "r2")
    await a.release("r1", "fill failed")
    rows = [json.loads(x) for x in (tmp_path / "control_audit.jsonl").read_text().splitlines()]
    rel = rows[-1]
    assert rel["a"] == "submit_release" and rel["ref"] == "r1" and rel["plat"] == "lever" and rel["reason"]
    b = mk(tmp_path, params=QUOTA)
    assert b.status()["quotas"]["day"]["used"] == 1
    assert (await _submit(b, "r3")).decision == "pass"
    assert not b.recent_blocks() or all(x["a"] != "submit_release" for x in b.recent_blocks())


async def test_later_resubmit_after_release_counts(tmp_path):
    a = mk(tmp_path)
    await _submit(a, "r1")
    await a.release("r1", "x")
    await _submit(a, "r1")
    assert a.state.count(actions=frozenset({"submit"})) == 1


class Spy:
    def __init__(self, release=True):
        self.released: list[tuple] = []
        if not release:
            self.release = None   # type: ignore[assignment]

    async def check(self, action, ctx):
        return {"decision": "pass"}

    async def release(self, ref, reason):
        self.released.append((ref, reason))


async def _run(status, spy):
    e, _ = make(adapter=FakeAdapter(submit_status=status))
    e.control = spy
    out = await e.plan(POSTING.ref)
    await e.submit(POSTING.ref, EXTRA, out.approval_id, dry_run=False)
    return e


@pytest.mark.parametrize("status", ["needs_human", "roadblock", "submitted", "incomplete", "awaiting_code"])
async def test_no_release_for_sent_or_ambiguous_outcomes(status):
    spy = Spy()
    await _run(status, spy)
    assert spy.released == []


async def test_release_for_not_sent_outcome_and_noop_without_method():
    spy = Spy()
    e = await _run("duplicate", spy)    # engine maps duplicate -> failed (nothing sent)
    assert e.approvals.state(POSTING.ref)["state"] == "failed"
    assert [r for r, _ in spy.released] == [POSTING.ref]
    await _run("duplicate", Spy(release=False))   # ControlLike without release: skipped, no crash


@pytest.mark.parametrize("dispatched,released", [(False, 1), (True, 0)])
async def test_exception_releases_only_before_dispatch(dispatched, released):
    spy = Spy()
    e, _ = make()
    e.control = spy

    async def boom(ctx, eff, dry):
        ctx.meta["dispatched"] = dispatched
        raise ValueError("bad")
    e._dispatch = boom
    out = await e.plan(POSTING.ref)
    await e.submit(POSTING.ref, EXTRA, out.approval_id, dry_run=False)
    assert len(spy.released) == released

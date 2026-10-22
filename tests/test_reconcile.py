"""ADR-0025: confirmation reconciliation (fictional fixtures only)."""
import json
from datetime import UTC, datetime
from pathlib import Path

import pytest

from job_apply_mcp.apply.approvals import ApprovalBook
from job_apply_mcp.config import Settings
from job_apply_mcp.ingest.confirmations import ConfirmationParser
from job_apply_mcp.ingest.reconcile import pending_summary, reconcile
from job_apply_mcp.ingest.sources import message_from_bytes
from job_apply_mcp.ingest.tools import reconcile_now
from job_apply_mcp.store.sqlite import SqliteStore

FIX = Path(__file__).parent / "fixtures" / "mail"
FIX2 = Path(__file__).parent / "fixtures" / "mail_recon"
T0 = datetime(2026, 10, 5, 9, 58, tzinfo=UTC).timestamp()   # two minutes before the fixture mail


def load(*names):
    return [message_from_bytes(next(p for p in (FIX / f"{n}.eml", FIX2 / f"{n}.eml") if p.exists()).read_bytes())
            for n in names]


class Clock:
    def __init__(self, t):
        self.t = t

    def __call__(self):
        return self.t


@pytest.fixture
def env(tmp_path):
    clk = Clock(T0)
    book = ApprovalBook(":memory:", clock=clk)
    store = SqliteStore(tmp_path / "d")
    yield book, store, Settings(), ConfirmationParser(), clk
    store.close()


def submit(book, ref, co, title, state="unknown"):
    book.set_state(ref, state, company=co, title=title, url="https://example.com/" + ref)


async def run(env, names, force=False):
    book, store, s, parser, clk = env
    return await reconcile(book, store, s, parser=parser, messages=load(*names), force=force, now=clk.t)


async def test_schedule_created_on_submit_states(env):
    book = env[0]
    for st in ("submitting", "unknown", "submitted", "handoff"):
        submit(book, f"greenhouse:nw:{st}", "Northwind Robotics", "Backend Engineer", st)
    submit(book, "greenhouse:nw:f", "Northwind Robotics", "Backend Engineer", "failed")
    assert len(book.open_reconciliations()) == 4
    assert pending_summary(book, now=T0)["open"] == 4
    assert pending_summary(book, now=T0)["next_due_min"] == 2.0


async def test_confident_match_confirms_with_subject_evidence(env):
    book, store, _, _, clk = env
    submit(book, "greenhouse:nw:1", "Northwind Robotics", "Backend Engineer")
    clk.t = T0 + 3 * 60
    out = await run(env, ["greenhouse"])
    assert [c["ref"] for c in out["confirmed"]] == ["greenhouse:nw:1"]
    st = book.state("greenhouse:nw:1")
    assert st["state"] == "confirmed"
    assert "Thank you for applying to Northwind Robotics" in st["detail"]
    assert "Hi Avery" not in st["detail"]                       # never bodies
    assert await store.seen("Northwind Robotics", "Backend Engineer")
    assert book.open_reconciliations() == []


async def test_confirmed_is_terminal_and_blocks_resubmit(env):
    book, *_ = env
    submit(book, "greenhouse:nw:1", "Northwind Robotics", "Backend Engineer")
    assert book.confirm("greenhouse:nw:1", "ev")
    assert book.begin_submit("greenhouse:nw:1", None) == (False, "confirmed")
    assert not book.set_state("greenhouse:nw:1", "failed")
    assert not book.resolve("greenhouse:nw:1", "failed", "x", "t")[0]


async def test_title_mismatch_and_wrong_company_do_not_match(env):
    book, _, _, _, clk = env
    submit(book, "greenhouse:nw:1", "Northwind Robotics", "Backend Engineer")
    submit(book, "greenhouse:qf:2", "Other Co", "Backend Engineer")
    clk.t = T0 + 180
    out = await run(env, ["greenhouse_other_role"])
    assert out["confirmed"] == []
    assert book.state("greenhouse:nw:1")["state"] == "unknown"


async def test_platform_conflict_and_time_window(env):
    book, _, _, _, clk = env
    submit(book, "lever:quill:1", "Northwind Robotics", "Backend Engineer")   # greenhouse mail cannot confirm lever
    clk.t = T0 + 180
    assert (await run(env, ["greenhouse"]))["confirmed"] == []
    submit(book, "greenhouse:nw:2", "Northwind Robotics", "Backend Engineer")
    book._db.execute("UPDATE reconcile SET t0=? WHERE ref='greenhouse:nw:2'", (T0 + 10 * 86400,))
    assert (await run(env, ["greenhouse"], force=True))["confirmed"] == []    # mail predates the submit


async def test_ambiguous_email_changes_nothing(env):
    book, _, _, _, clk = env
    submit(book, "greenhouse:nw:1", "Northwind Robotics", "Backend Engineer")
    submit(book, "greenhouse:nw:2", "Northwind Robotics", "Backend Engineer")
    clk.t = T0 + 180
    out = await run(env, ["greenhouse"])
    assert out["confirmed"] == [] and out["ambiguous"] == 2
    assert book.state("greenhouse:nw:1")["state"] == "unknown"


async def test_unknown_without_match_stays_unknown_and_is_reported(env):
    book, _, _, _, clk = env
    submit(book, "greenhouse:nw:1", "Northwind Robotics", "Backend Engineer")
    for minutes in (3, 11, 61):
        clk.t = T0 + minutes * 60
        out = await run(env, ["lever"])
    assert [u["ref"] for u in out["unresolved"]] == ["greenhouse:nw:1"]
    st = book.state("greenhouse:nw:1")
    assert st["state"] == "unknown" and "no confirmation" in st["detail"]
    assert pending_summary(book, now=clk.t)["open"] == 0


async def test_nothing_due_is_noop_unless_forced(env):
    book, _, _, _, clk = env
    submit(book, "greenhouse:nw:1", "Northwind Robotics", "Backend Engineer")
    clk.t = T0 + 30
    assert "nothing due" in (await run(env, ["greenhouse"]))["note"]
    assert (await run(env, ["greenhouse"], force=True))["confirmed"][0]["from"] == "unknown"


class Svc:
    def __init__(self, book, store, settings):
        self.engine = type("E", (), {"approvals": book})()
        self.store, self.settings = store, settings


async def test_client_source_returns_client_task(env):
    book, store, s, _, _ = env
    submit(book, "greenhouse:nw:1", "Northwind Robotics", "Backend Engineer", "submitted")
    out = await reconcile_now(Svc(book, store, s))
    assert out["client_task"]["task"] == "fetch_confirmation_emails" and out["open"] == 1
    assert book.state("greenhouse:nw:1")["state"] == "submitted"


async def test_mbox_source_reconciles(env, tmp_path):
    book, store, s, _, clk = env
    s.ingest.source, s.ingest.path = "mbox", str(FIX)
    s.ingest.reconcile_window_hours = 24 * 365 * 5
    submit(book, "greenhouse:nw:1", "Northwind Robotics", "Backend Engineer")
    clk.t = T0 + 180
    out = await reconcile_now(Svc(book, store, s), force=True)
    assert out["confirmed"][0]["ref"] == "greenhouse:nw:1"


def test_pending_in_control_status(tmp_path):
    from job_apply_mcp.control.tools import register

    s = Settings()
    s.paths.data_dir = str(tmp_path)
    b = ApprovalBook(tmp_path)
    submit(b, "greenhouse:nw:1", "Northwind Robotics", "Backend Engineer")

    class M:
        def __init__(self):
            self.fns = {}

        def tool(self):
            return lambda f: self.fns.setdefault(f.__name__, f)

    m = M()
    register(m, lambda: s)
    import asyncio

    st = json.loads(asyncio.run(m.fns["control_status"]()))
    assert st["pending_reconciliations"]["open"] == 1

import asyncio
import time

import platform_fixtures as fx
import respx

from job_apply_mcp.config import Settings
from job_apply_mcp.core.concurrency import HostLimiter
from job_apply_mcp.search.sessions import SessionError, SessionStore, parse_cursor
from job_apply_mcp.services import Services

GH = "https://boards-api.greenhouse.io/v1/boards/acmeco/jobs"


def svc(tmp_path, sessions=None, **conc):
    return Services(Settings.model_validate({
        "paths": {"data_dir": str(tmp_path)}, "concurrency": {"min_interval_s": 0, **conc},
        "search": {"sessions": sessions or {}},
        "platforms": {"backoff_s": 0, "boards": {"greenhouse": ["acmeco"], "lever": ["democo"], "ashby": ["exampleai"]}},
    }))


def mock_all():
    gh = respx.get(GH).respond(json=fx.GH_LIST)
    respx.get("https://api.lever.co/v0/postings/democo").respond(json=fx.LEVER_LIST)
    respx.get("https://api.ashbyhq.com/posting-api/job-board/exampleai").respond(json=fx.ASHBY_BOARD)
    return gh


@respx.mock
async def test_page_two_does_not_refetch(tmp_path):
    gh = mock_all()
    s = svc(tmp_path)
    p1 = await s.search(since_days=0, apply_policy=False, limit=2)
    calls = respx.calls.call_count
    assert calls >= 3 and gh.call_count == 1
    sid, off = parse_cursor(p1["next"])
    assert sid == p1["session"] and off == 2
    p2 = await s.search(since_days=0, apply_policy=False, limit=2, cursor=p1["next"])
    assert respx.calls.call_count == calls   # no network on follow-up pages
    assert p2["next"] is None and {r["ref"] for r in p1["rows"]}.isdisjoint(r["ref"] for r in p2["rows"])
    assert p2["total"] == p1["total"] == 4
    await s.aclose()


@respx.mock
async def test_status_and_progress_callback(tmp_path):
    mock_all()
    s = svc(tmp_path)
    seen = []

    async def prog(done, total):
        seen.append((done, total))

    p1 = await s.search(since_days=0, apply_policy=False, progress=prog)
    assert seen[-1] == (3, 3)
    st = s.search_status(p1["session"])
    assert st["status"] == "done" and st["boards_done"] == 3 and st["total"] == 4
    assert "error" in s.search_status("deadbeef00")
    await s.aclose()


@respx.mock
async def test_expired_and_invalid_cursor(tmp_path):
    mock_all()
    s = svc(tmp_path, sessions={"ttl_s": 60})
    clock = [1000.0]
    s.sessions._now = lambda: clock[0]
    p1 = await s.search(since_days=0, apply_policy=False, limit=2)
    clock[0] += 61
    out = await s.search(since_days=0, apply_policy=False, limit=2, cursor=p1["next"])
    assert out["rows"] == [] and "expired" in out["error"] and "without a cursor" in out["error"]
    bad = await s.search(cursor="../etc:1")
    assert "invalid cursor" in bad["error"]
    await s.aclose()


def test_lru_eviction_and_disk_persistence(tmp_path):
    st = SessionStore(ttl_s=100, max_sessions=2, directory=tmp_path)
    a = st.create()
    a.status, a.rows = "done", [{"ref": "x"}]
    st.save(a)
    b, c = st.create(), st.create()
    assert a.sid not in st._mem
    again = SessionStore(ttl_s=100, max_sessions=2, directory=tmp_path).get(a.sid)   # reloaded from disk
    assert again.rows == [{"ref": "x"}]
    try:
        SessionStore(ttl_s=0, max_sessions=2, directory=tmp_path, clock=lambda: time.time() + 5).get(a.sid)
        raise AssertionError("should expire")
    except SessionError:
        pass
    assert b.sid in st._mem and c.sid in st._mem


async def test_per_host_override_and_default_cfg():
    cfg = Settings.model_validate({}).concurrency
    assert cfg.hosts["boards-api.greenhouse.io"].per_host == 16 and cfg.per_host == 4
    lim = HostLimiter(per_host=1, min_interval_s=0.0, overrides={"fast.example.com": (3, 0.0)})
    active = {"fast": 0, "slow": 0, "max_fast": 0, "max_slow": 0}

    async def hit(url, key):
        async with await lim(url):
            active[key] += 1
            active["max_" + key] = max(active["max_" + key], active[key])
            await asyncio.sleep(0.01)
            active[key] -= 1

    await asyncio.gather(*[hit("https://fast.example.com:443/x", "fast") for _ in range(9)],
                         *[hit("https://slow.example.com/x", "slow") for _ in range(4)])
    assert active["max_fast"] == 3 and active["max_slow"] == 1


async def test_spacing_is_per_host_not_global():
    lim = HostLimiter(per_host=8, min_interval_s=0.0, overrides={"a.example.com": (8, 0.05), "b.example.com": (8, 0.05)})

    async def hit(url):
        async with await lim(url):
            pass

    t = time.monotonic()
    await asyncio.gather(*[hit("https://a.example.com/")] * 1, *[hit("https://b.example.com/")] * 1,
                         hit("https://a.example.com/"), hit("https://b.example.com/"))
    assert time.monotonic() - t < 0.12   # two hosts spaced in parallel: ~0.05s, not 0.15s


@respx.mock
async def test_error_in_one_board_is_recorded_in_session(tmp_path):
    mock_all()
    respx.get("https://api.ashbyhq.com/posting-api/job-board/gone").respond(404)
    s = svc(tmp_path)
    out = await s.search(boards=["greenhouse:acmeco", "ashby:gone"], since_days=0, apply_policy=False, limit=1)
    assert out["errors"][0]["board"] == "ashby:gone"
    assert s.search_status(out["session"])["boards_failed"] == 1
    await s.aclose()

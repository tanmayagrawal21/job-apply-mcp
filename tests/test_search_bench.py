"""Search fan-out benchmark with a fake transport (ADR-0043). Offline, deterministic payloads, fictional boards.

Cold: every board is fetched through the HostLimiter with simulated latency. Warm (inside cache_ttl_s): no request,
no limiter slot, no disk read, no re-parse; the board's finished postings come from the in-memory memo.
"""
import asyncio
import html
import time
from datetime import UTC, datetime, timedelta

import httpx

from job_apply_mcp.config import Settings
from job_apply_mcp.core.concurrency import HostLimiter
from job_apply_mcp.platforms import HttpClient
from job_apply_mcp.search.ranking import rank_postings
from job_apply_mcp.services import Services

N_GH, N_ASHBY, N_LEVER, JOBS = 120, 80, 40, 15
LATENCY_S = 0.01
TODAY = datetime.now(UTC)
DESC = ("Build reliable services in Python and Go. 2+ years of experience. Pay range: $120,000 - $150,000 per year. "
        "We collaborate across teams in Springfield. ") * 8


def _iso(days: int) -> str:
    return (TODAY - timedelta(days=days)).strftime("%Y-%m-%dT%H:%M:%S+00:00")


def _gh(board: str) -> dict:
    return {"jobs": [{"id": i, "title": f"Software Engineer {i}", "updated_at": _iso(i * 3),
                      "absolute_url": f"https://job-boards.greenhouse.io/{board}/jobs/{i}",
                      "location": {"name": "Remote, US"}, "company_name": board,
                      "content": html.escape(f"<p>{DESC}</p>")} for i in range(JOBS)]}


def _ashby(board: str) -> dict:
    return {"jobs": [{"id": f"{board}-{i}", "title": f"Data Engineer {i}", "location": "Springfield",
                      "publishedAt": _iso(i * 3), "isListed": True, "descriptionPlain": DESC,
                      "jobUrl": f"https://jobs.ashbyhq.com/{board}/{board}-{i}"} for i in range(JOBS)]}


def _lever(board: str) -> list:
    return [{"id": f"{board}-{i}", "text": f"Backend Engineer {i}", "categories": {"location": "Springfield"},
             "createdAt": int((TODAY - timedelta(days=i * 3)).timestamp() * 1000), "descriptionPlain": DESC,
             "lists": [], "additionalPlain": "", "hostedUrl": f"https://jobs.lever.co/{board}/{board}-{i}"}
            for i in range(JOBS)]


class FakeTransport(httpx.AsyncBaseTransport):
    """Board payloads by URL, with latency; a `gated` board blocks until its event is set; counts requests per host."""

    def __init__(self, gated: dict[str, asyncio.Event] | None = None):
        self.calls: dict[str, int] = {}
        self.gated = gated or {}

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        host, path = request.url.host, request.url.path
        self.calls[host] = self.calls.get(host, 0) + 1
        board = path.rstrip("/").split("/")[-2 if path.endswith("/jobs") else -1]
        if board in self.gated:
            await self.gated[board].wait()
        else:
            await asyncio.sleep(LATENCY_S)
        if host == "boards-api.greenhouse.io":
            body = _gh(board)
        elif host == "api.ashbyhq.com":
            body = _ashby(board)
        else:
            body = _lever(board)
        return httpx.Response(200, json=body, headers={"etag": f'"{board}"'})


def _boards() -> list[str]:
    return ([f"greenhouse:gh{i:03d}" for i in range(N_GH)] + [f"ashby:ab{i:03d}" for i in range(N_ASHBY)]
            + [f"lever:lv{i:03d}" for i in range(N_LEVER)])


def _svc(tmp_path, transport: httpx.AsyncBaseTransport, **search) -> Services:
    s = Services(Settings.model_validate({
        "paths": {"data_dir": str(tmp_path)},
        "search": {"rank_candidates": 0, **search},
        "platforms": {"backoff_s": 0, "retries": 0},
    }))
    p = s.settings.platforms
    s._http = HttpClient(cache_dir=tmp_path / "http_cache", ttl_s=p.cache_ttl_s, limiter=s.hosts, backoff_s=0,
                         retries=0, client=httpx.AsyncClient(transport=transport))
    return s


class CountingLimiter(HostLimiter):
    def __init__(self, *a, **kw):
        super().__init__(*a, **kw)
        self.slots = 0

    async def __call__(self, url):
        self.slots += 1
        return await super().__call__(url)


async def test_cold_then_warm_search_skips_network_limiter_and_parse(tmp_path):
    tr = FakeTransport()
    s = _svc(tmp_path, tr)
    lim = CountingLimiter(per_host=s.hosts.per_host, min_interval_s=0, overrides={
        h: (v[0], 0.0) for h, v in s.hosts.overrides.items()})   # spacing off: this measures our overhead
    s._http.limiter = s.hosts = lim
    boards = _boards()

    t = time.perf_counter()
    cold = await s.search(query="engineer", boards=boards, since_days=21, apply_policy=False, limit=50)
    cold_s = time.perf_counter() - t
    assert sum(tr.calls.values()) == len(boards) and lim.slots == len(boards)
    tm = cold["timings"]
    assert {"total_s", "fetch_s", "rank_s", "network_s", "cache_s", "derive_s", "n", "slowest"} <= set(tm)
    assert tm["n"]["requests"] == len(boards) and tm["n"]["memo_hits"] == 0
    assert cold["total"] == len(boards) * 8   # since_days=21 is inclusive: jobs posted 0,3,...,21 days ago

    t = time.perf_counter()
    warm = await s.search(query="engineer", boards=boards, since_days=21, apply_policy=False, limit=50)
    warm_s = time.perf_counter() - t
    assert sum(tr.calls.values()) == len(boards) and lim.slots == len(boards)   # no request, no limiter slot
    assert warm["timings"]["n"] == {"requests": 0, "cache_hits": 0, "memo_hits": len(boards), "revalidated": 0}
    assert warm["timings"]["derive_s"] == 0 and warm["total"] == cold["total"]
    assert [r["ref"] for r in warm["rows"]] == [r["ref"] for r in cold["rows"]]
    print(f"\nbench boards={len(boards)} cold={cold_s:.2f}s warm={warm_s:.3f}s timings={warm['timings']}")

    # a new process (empty memo) reads the disk cache: still no network, and parses each board once
    s2 = _svc(tmp_path, tr)
    again = await s2.search(query="engineer", boards=boards, since_days=21, apply_policy=False, limit=50)
    assert sum(tr.calls.values()) == len(boards) and again["timings"]["n"]["cache_hits"] == len(boards)
    await s.aclose()
    await s2.aclose()


async def test_revalidation_304_keeps_memo_and_runs_concurrently(tmp_path):
    now = [1_000_000.0]
    inflight, peak = [0], [0]

    class Reval(httpx.AsyncBaseTransport):
        async def handle_async_request(self, request):
            inflight[0] += 1
            peak[0] = max(peak[0], inflight[0])
            await asyncio.sleep(0.02)
            inflight[0] -= 1
            if request.headers.get("if-none-match"):
                return httpx.Response(304)
            return httpx.Response(200, json=_gh("x"), headers={"etag": '"v1"'})

    c = HttpClient(cache_dir=tmp_path, ttl_s=60, backoff_s=0, clock=lambda: now[0],
                   limiter=HostLimiter(per_host=8), client=httpx.AsyncClient(transport=Reval()))
    calls = [0]

    def derive(d):
        calls[0] += 1
        return len(d["jobs"])

    urls = [f"https://boards-api.greenhouse.io/v1/boards/b{i}/jobs" for i in range(16)]
    assert await asyncio.gather(*(c.get_derived(u, derive) for u in urls)) == [JOBS] * 16
    now[0] += 120   # stale: every board revalidates with If-None-Match, in parallel up to per_host
    peak[0] = 0
    assert await asyncio.gather(*(c.get_derived(u, derive) for u in urls)) == [JOBS] * 16
    assert c.stats["revalidated"] == 16 and calls[0] == 16 and peak[0] > 1   # 304: memo reused, no re-derive
    await c.aclose()


async def test_slow_board_times_out_without_stalling_the_fanout(tmp_path):
    release = asyncio.Event()   # the slow board answers only when the test says so: no timing race
    tr = FakeTransport(gated={"gh001": release})
    s = _svc(tmp_path, tr, board_timeout_s=2.0)
    boards = ["greenhouse:gh000", "greenhouse:gh001", "ashby:ab000"]
    out = await s.search(boards=boards, since_days=0, apply_policy=False)
    assert out["errors"] == [{"board": "greenhouse:gh001", "err": "timed out after 2s"}]
    assert out["total"] == 2 * JOBS and out["timings"]["slowest"][0][0] == "greenhouse:gh001"
    late = next(iter(s._late))
    assert not late.done()   # the search returned without waiting for the slow board, whose fetch keeps running ...
    release.set()
    await asyncio.wait_for(asyncio.shield(late), 30)   # wait for the background fetch itself, not a fixed sleep
    await asyncio.sleep(0)
    assert not s._late
    again = await s.search(boards=boards, since_days=0, apply_policy=False)
    assert "errors" not in again and again["total"] == 3 * JOBS   # ... and the next search reads it from cache
    assert sum(tr.calls.values()) == 3
    await s.aclose()


def test_ranking_work_is_linear_in_rows(monkeypatch):
    """Deterministic, no wall clock: tokenization calls grow exactly 4x for 4x rows (a per-row pass over all rows,
    i.e. quadratic work, would grow 16x)."""
    from job_apply_mcp.config import SearchCfg
    from job_apply_mcp.core.models import Posting
    from job_apply_mcp.search import ranking

    calls = [0]
    real = ranking.tokens

    def counting(text):
        calls[0] += 1
        return real(text)

    monkeypatch.setattr(ranking, "tokens", counting)

    def work(n):
        rs = [Posting(platform="greenhouse", ref=f"greenhouse:b:{i}", company=f"co{i % 300}",
                      title=f"Software Engineer {i % 7}", location="Remote", url=f"https://example.com/{i}",
                      posted_at="2026-10-01", description=DESC if i % 5 == 0 else None) for i in range(n)]
        calls[0] = 0
        out = rank_postings(rs, "software engineer", ["python"], SearchCfg())
        assert len(out) == n
        return calls[0]

    small, big = work(500), work(2000)
    fixed = work(0)   # per-call setup (query and skill tokens)
    assert big - fixed == 4 * (small - fixed)


async def test_targets_are_interleaved_across_platforms(tmp_path):
    s = _svc(tmp_path, FakeTransport())
    targets, _ = s._targets(["greenhouse:g1", "greenhouse:g2", "greenhouse:g3", "ashby:a1", "lever:l1"], None)
    assert [f"{a.name}:{b}" for a, b in targets] == [
        "greenhouse:g1", "ashby:a1", "lever:l1", "greenhouse:g2", "greenhouse:g3"]
    await s.aclose()

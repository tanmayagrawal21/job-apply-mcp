import asyncio
import time

import pytest

from job_apply_mcp.config import load_settings
from job_apply_mcp.core.concurrency import HostLimiter, bounded_gather
from job_apply_mcp.core.registry import Registry


async def test_bounded_gather_order_cap_and_errors():
    running = 0
    peak = 0

    async def job(i):
        nonlocal running, peak
        running += 1
        peak = max(peak, running)
        await asyncio.sleep(0.01)
        running -= 1
        if i == 3:
            raise ValueError("boom")
        return i

    out = await bounded_gather((job(i) for i in range(10)), limit=3)
    assert peak <= 3
    assert [o for o in out if not isinstance(o, BaseException)] == [0, 1, 2, 4, 5, 6, 7, 8, 9]
    assert isinstance(out[3], ValueError)


async def test_host_limiter_spacing():
    lim = HostLimiter(per_host=1, min_interval_s=0.05)
    t0 = time.monotonic()
    for _ in range(3):
        async with await lim("https://example.com/a"):
            pass
    assert time.monotonic() - t0 >= 0.08  # slack for coarse Windows timers


def test_registry_duplicate_and_unknown():
    r = Registry("thing")
    r.register("a")(lambda: 1)
    with pytest.raises(ValueError):
        r.register("a")(lambda: 2)
    with pytest.raises(KeyError):
        r.create("b")
    assert r.create("a") == 1


def test_config_precedence(tmp_path, monkeypatch):
    user = tmp_path / "user.toml"
    user.write_text('dry_run = false\n[concurrency]\nper_host = 2\n')
    (tmp_path / "job-apply-mcp.toml").write_text('[concurrency]\nper_host = 3\n')
    monkeypatch.setenv("JOB_APPLY_MCP_CONFIG", str(user))
    monkeypatch.setenv("JOB_APPLY__CONCURRENCY__GLOBAL_LIMIT", "7")
    s = load_settings(tmp_path)
    assert s.dry_run is False and s.concurrency.per_host == 3 and s.concurrency.global_limit == 7

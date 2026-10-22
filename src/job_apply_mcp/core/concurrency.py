"""Parallelism primitives (ADR-0002). Everything I/O-bound is async; limits are configured, not hard-coded.

- `bounded_gather`: run many coroutines with a global concurrency cap, preserving input order,
  returning exceptions as values so one failure never cancels a batch.
- `HostLimiter`: per-host semaphores + minimum spacing, so fan-out over hundreds of boards stays polite.
"""
from __future__ import annotations

import asyncio
import time
from collections import defaultdict
from collections.abc import Awaitable, Iterable
from typing import TypeVar
from urllib.parse import urlparse

T = TypeVar("T")


async def bounded_gather(aws: Iterable[Awaitable[T]], limit: int) -> list[T | BaseException]:
    sem = asyncio.Semaphore(max(1, limit))

    async def run(aw: Awaitable[T]) -> T | BaseException:
        async with sem:
            try:
                return await aw
            except Exception as e:  # noqa: BLE001 - errors are returned, not raised
                return e

    return await asyncio.gather(*(run(a) for a in aws))


class HostLimiter:
    """Per-host concurrency + spacing. `overrides` maps a hostname (no port) to (per_host, min_interval_s) so a
    single high-capacity public API (e.g. all Greenhouse boards) is not throttled by the polite global default
    (ADR-0024). Spacing slots are reserved under a per-host lock and slept outside it, so hosts never block
    each other and a host's own requests are spaced exactly `min_interval_s` apart."""

    def __init__(self, per_host: int = 4, min_interval_s: float = 0.0,
                 overrides: dict[str, tuple[int, float]] | None = None):
        self.per_host = per_host
        self.min_interval_s = min_interval_s
        self.overrides = {h.lower(): v for h, v in (overrides or {}).items()}
        self._sems: dict[str, asyncio.Semaphore] = {}
        self._next: dict[str, float] = defaultdict(float)
        self._locks: dict[str, asyncio.Lock] = defaultdict(asyncio.Lock)

    def limits(self, host: str) -> tuple[int, float]:
        return self.overrides.get(host.lower(), (self.per_host, self.min_interval_s))

    def _sem(self, host: str) -> asyncio.Semaphore:
        if host not in self._sems:
            self._sems[host] = asyncio.Semaphore(max(1, self.limits(host)[0]))
        return self._sems[host]

    async def __call__(self, url: str):
        return _HostSlot(self, urlparse(url).hostname or urlparse(url).netloc)


class _HostSlot:
    def __init__(self, lim: HostLimiter, host: str):
        self.lim, self.host = lim, host

    async def __aenter__(self):
        await self.lim._sem(self.host).acquire()
        interval = self.lim.limits(self.host)[1]
        if interval:
            async with self.lim._locks[self.host]:
                now = time.monotonic()
                at = max(now, self.lim._next[self.host])
                self.lim._next[self.host] = at + interval
            if at > now:
                await asyncio.sleep(at - now)

    async def __aexit__(self, *exc):
        self.lim._sem(self.host).release()

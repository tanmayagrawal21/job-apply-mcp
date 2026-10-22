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
    def __init__(self, per_host: int = 4, min_interval_s: float = 0.0):
        self.per_host = per_host
        self.min_interval_s = min_interval_s
        self._sems: dict[str, asyncio.Semaphore] = defaultdict(lambda: asyncio.Semaphore(self.per_host))
        self._last: dict[str, float] = defaultdict(float)
        self._lock = asyncio.Lock()

    async def __call__(self, url: str):
        return _HostSlot(self, urlparse(url).netloc)


class _HostSlot:
    def __init__(self, lim: HostLimiter, host: str):
        self.lim, self.host = lim, host

    async def __aenter__(self):
        await self.lim._sems[self.host].acquire()
        if self.lim.min_interval_s:
            async with self.lim._lock:
                wait = self.lim._last[self.host] + self.lim.min_interval_s - time.monotonic()
                if wait > 0:
                    await asyncio.sleep(wait)
                self.lim._last[self.host] = time.monotonic()

    async def __aexit__(self, *exc):
        self.lim._sems[self.host].release()

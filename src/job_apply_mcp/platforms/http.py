"""Shared async HTTP client for all platform adapters (ADR-0007, builds on ADR-0002 and ADR-0005).

- One lazily created `httpx.AsyncClient` per process (keep-alive, shared connection pool).
- Every request goes through the `HostLimiter` (per-host concurrency + spacing).
- Idempotent requests (GET, and POST searches flagged `cache=True`) are cached on disk under
  `<data_dir>/http_cache`: fresh entries are served without a request; stale ones are revalidated with
  `If-None-Match`; a failing network falls back to the stale entry (stale-if-error).
- Idempotent requests retry on transport errors / 429 / 5xx with backoff. Form submissions never retry.
- Authenticated requests are never cached, and error messages never contain query strings (Lever keys).
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import os
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any

import httpx

from ..core.concurrency import HostLimiter


class HttpError(Exception):
    def __init__(self, status: int, url: str, body: str = ""):
        self.status, self.body = status, body[:500]
        super().__init__(f"HTTP {status} for {url.split('?')[0]}")


class HttpClient:
    def __init__(self, *, cache_dir: Path | None = None, ttl_s: int = 900, limiter: HostLimiter | None = None,
                 timeout_s: float = 20.0, retries: int = 2, backoff_s: float = 0.5,
                 user_agent: str = "job-apply-mcp", prune_days: int = 7,
                 client: httpx.AsyncClient | None = None, clock: Callable[[], float] = time.time):
        self.cache_dir, self.ttl_s, self.limiter = cache_dir, ttl_s, limiter
        self.timeout_s, self.retries, self.backoff_s = timeout_s, retries, backoff_s
        self.user_agent, self.prune_days, self.clock = user_agent, prune_days, clock
        self._client = client
        self._pruned = False
        self.stats = {"requests": 0, "cache_hits": 0, "revalidated": 0, "stale_served": 0}

    # ------------------------------------------------------------------ lifecycle
    def _http(self) -> httpx.AsyncClient:
        if self._client is None:
            self._client = httpx.AsyncClient(timeout=self.timeout_s, follow_redirects=True,
                                             headers={"User-Agent": self.user_agent})
        return self._client

    async def aclose(self) -> None:
        if self._client is not None:
            await self._client.aclose()
            self._client = None

    # ------------------------------------------------------------------ public API
    async def get_json(self, url: str, *, params: dict[str, Any] | None = None, headers: dict[str, str] | None = None,
                       cache: bool = True, ttl_s: int | None = None) -> Any:
        return await self._json("GET", url, params=params, headers=headers, cache=cache, ttl_s=ttl_s)

    async def post_json(self, url: str, *, json_body: Any, params: dict[str, Any] | None = None,
                        headers: dict[str, str] | None = None, cache: bool = True, ttl_s: int | None = None) -> Any:
        """POST a JSON body for an idempotent query (search, GraphQL read). Cached and retried like GET."""
        return await self._json("POST", url, params=params, headers=headers, json_body=json_body,
                                cache=cache, ttl_s=ttl_s)

    async def post_form(self, url: str, *, data: dict[str, Any] | None = None, files: dict[str, Any] | None = None,
                        params: dict[str, Any] | None = None, auth: tuple[str, str] | None = None) -> Any:
        """Non-idempotent multipart/form POST (application submission). Never cached, never retried."""
        resp = await self._send("POST", url, params=params, data=data, files=files, auth=auth, retry=False)
        return self._decode(resp)

    # ------------------------------------------------------------------ internals
    async def _json(self, method: str, url: str, *, params=None, headers=None, json_body=None,
                    cache: bool = True, ttl_s: int | None = None) -> Any:
        key = self._key(method, url, params, json_body) if (cache and self.cache_dir) else None
        entry = self._load(key) if key else None
        ttl = self.ttl_s if ttl_s is None else ttl_s
        if entry and self.clock() - entry["at"] < ttl:
            self.stats["cache_hits"] += 1
            return json.loads(entry["body"])
        hdrs = dict(headers or {})
        if entry and entry.get("etag"):
            hdrs["If-None-Match"] = entry["etag"]
        try:
            resp = await self._send(method, url, params=params, headers=hdrs, json_body=json_body, retry=True)
        except (HttpError, httpx.TransportError) as e:
            if entry and (isinstance(e, httpx.TransportError) or e.status >= 500):
                self.stats["stale_served"] += 1
                return json.loads(entry["body"])
            raise
        if resp.status_code == 304 and entry:
            self.stats["revalidated"] += 1
            entry["at"] = self.clock()
            self._store(key, entry)
            return json.loads(entry["body"])
        if key:
            self._store(key, {"at": self.clock(), "etag": resp.headers.get("etag"), "body": resp.text})
        return self._decode(resp)

    async def _send(self, method: str, url: str, *, retry: bool, params=None, headers=None, json_body=None,
                    data=None, files=None, auth=None) -> httpx.Response:
        attempts = (self.retries if retry else 0) + 1
        for i in range(attempts):
            try:
                if self.limiter:
                    async with await self.limiter(url):
                        resp = await self._request(method, url, params, headers, json_body, data, files, auth)
                else:
                    resp = await self._request(method, url, params, headers, json_body, data, files, auth)
            except httpx.TransportError:
                if i + 1 >= attempts:
                    raise
                await asyncio.sleep(self.backoff_s * (2 ** i))
                continue
            self.stats["requests"] += 1
            if resp.status_code == 304:
                return resp
            if resp.status_code in (429, 500, 502, 503, 504) and i + 1 < attempts:
                wait = self.backoff_s * (2 ** i)
                ra = resp.headers.get("retry-after")
                if ra and ra.isdigit():
                    wait = min(float(ra), 10.0)
                await asyncio.sleep(wait)
                continue
            if resp.status_code >= 400:
                raise HttpError(resp.status_code, url, resp.text)
            return resp
        raise RuntimeError("unreachable")  # pragma: no cover

    async def _request(self, method, url, params, headers, json_body, data, files, auth) -> httpx.Response:
        return await self._http().request(method, url, params=params, headers=headers, json=json_body,
                                          data=data, files=files, auth=auth)

    @staticmethod
    def _decode(resp: httpx.Response) -> Any:
        try:
            return resp.json()
        except ValueError:
            return resp.text

    # ------------------------------------------------------------------ disk cache
    @staticmethod
    def _key(method: str, url: str, params, json_body) -> str:
        u = httpx.URL(url).copy_merge_params(params or {})
        raw = f"{method} {u}\n{json.dumps(json_body, sort_keys=True) if json_body is not None else ''}"
        return hashlib.sha256(raw.encode()).hexdigest()[:40]

    def _path(self, key: str) -> Path:
        assert self.cache_dir is not None
        return self.cache_dir / f"{key}.json"

    def _load(self, key: str | None) -> dict | None:
        if not key or not self.cache_dir:
            return None
        self._prune_once()
        try:
            return json.loads(self._path(key).read_text())
        except (OSError, ValueError):
            return None

    def _store(self, key: str | None, entry: dict) -> None:
        if not key or not self.cache_dir:
            return
        try:
            self.cache_dir.mkdir(parents=True, exist_ok=True)
            tmp = self._path(key).with_suffix(f".{os.getpid()}.tmp")
            tmp.write_text(json.dumps(entry))
            tmp.replace(self._path(key))
        except OSError:
            pass  # cache is best-effort

    def _prune_once(self) -> None:
        if self._pruned or not self.cache_dir:
            return
        self._pruned = True
        cutoff = self.clock() - self.prune_days * 86400
        try:
            for p in self.cache_dir.glob("*.json"):
                try:
                    if json.loads(p.read_text()).get("at", 0) < cutoff:
                        p.unlink()
                except (OSError, ValueError):
                    p.unlink(missing_ok=True)
        except OSError:
            pass

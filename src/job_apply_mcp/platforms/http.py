"""Shared async HTTP client for all platform adapters (ADR-0007, builds on ADR-0002 and ADR-0005).

- One lazily created `httpx.AsyncClient` per process (keep-alive, shared connection pool).
- Every request goes through the `HostLimiter` (per-host concurrency + spacing).
- Idempotent requests (GET, and POST searches flagged `cache=True`) are cached on disk under
  `<data_dir>/http_cache`: fresh entries are served without a request; stale ones are revalidated with
  `If-None-Match`; a failing network falls back to the stale entry (stale-if-error). A 404 is cached as a
  negative entry for the same TTL, so dead boards cost one request per TTL, not one per search (ADR-0043).
- Idempotent requests retry on transport errors / 429 / 5xx with backoff. Form submissions never retry.
- Authenticated requests are never cached, and error messages never contain query strings (Lever keys).
- `get_derived` memoizes a value computed from a cached response (e.g. a board's finished postings) in memory,
  keyed by the response's content digest, so a repeat search inside the TTL (or after a 304) skips the disk
  read, the JSON decode and the per-posting text work entirely (ADR-0043).
- `get_text` streams the body and stops reading after `max_bytes` (default `MAX_TEXT_BYTES`, 2 MB), so a huge or
  endless page cannot exhaust memory; the timeout, retries and rate limit are the same as any GET (ADR-0072).
- `timings` accumulates seconds spent on the network (inside the limiter slot), in cache lookups (disk read +
  decode) and in derive functions; they are summed over concurrent requests, so they can exceed wall time.
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import os
import time
from collections import OrderedDict
from collections.abc import Callable
from pathlib import Path
from typing import Any, TypeVar

import httpx

from ..core.concurrency import HostLimiter

T = TypeVar("T")

MAX_TEXT_BYTES = 2_000_000   # get_text stops reading a body after this many (decoded) bytes


class HttpError(Exception):
    def __init__(self, status: int, url: str, body: str = ""):
        self.status, self.body = status, body[:500]
        from ..core.secrets import safe_url
        super().__init__(f"HTTP {status} for {safe_url(url)}")   # no userinfo / query / fragment (ADR-0074)


class HttpClient:
    def __init__(self, *, cache_dir: Path | None = None, ttl_s: int = 900, limiter: HostLimiter | None = None,
                 timeout_s: float = 20.0, retries: int = 2, backoff_s: float = 0.5,
                 user_agent: str = "job-apply-mcp", prune_days: int = 7, memo_entries: int = 4096,
                 client: httpx.AsyncClient | None = None, clock: Callable[[], float] = time.time):
        self.cache_dir, self.ttl_s, self.limiter = cache_dir, ttl_s, limiter
        self.timeout_s, self.retries, self.backoff_s = timeout_s, retries, backoff_s
        self.user_agent, self.prune_days, self.clock = user_agent, prune_days, clock
        self._client = client
        self._pruned = False
        self.memo_entries = memo_entries
        self._meta: dict[str, dict] = {}                          # key -> {"at", "digest"} of the disk entry
        self._memo: OrderedDict[tuple[str, str], tuple[str, Any]] = OrderedDict()  # (key, tag) -> (digest, value)
        self.stats = {"requests": 0, "cache_hits": 0, "revalidated": 0, "stale_served": 0, "memo_hits": 0}
        self.timings = {"network_s": 0.0, "cache_s": 0.0, "derive_s": 0.0}

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

    async def get_derived(self, url: str, derive: Callable[[Any], T], *, tag: str = "",
                          params: dict[str, Any] | None = None, ttl_s: int | None = None) -> T:
        """`derive(get_json(url))`, memoized in memory by the response's content digest. A fresh entry whose digest
        matches returns the memo without touching disk or network; a 304 keeps the digest, so the memo survives
        revalidation. Without a cache dir this is a plain `derive(get_json(...))`."""
        if not self.cache_dir:
            data = await self.get_json(url, params=params, ttl_s=ttl_s)
            return self._derive(derive, data)
        key = self._key("GET", url, params, None)
        ttl = self.ttl_s if ttl_s is None else ttl_s
        meta, memo = self._meta.get(key), self._memo.get((key, tag))
        if meta and memo and memo[0] == meta["digest"] and self.clock() - meta["at"] < ttl:
            self.stats["memo_hits"] += 1
            self._memo.move_to_end((key, tag))
            return memo[1]
        data = await self.get_json(url, params=params, ttl_s=ttl_s)
        meta, memo = self._meta.get(key), self._memo.get((key, tag))
        if meta and memo and memo[0] == meta["digest"]:   # e.g. a 304, or a concurrent caller derived it already
            self.stats["memo_hits"] += 1
            return memo[1]
        value = self._derive(derive, data)
        if meta:
            self._memo[(key, tag)] = (meta["digest"], value)
            while len(self._memo) > self.memo_entries:
                self._memo.popitem(last=False)
        return value

    def _derive(self, derive: Callable[[Any], T], data: Any) -> T:
        t = time.perf_counter()
        try:
            return derive(data)
        finally:
            self.timings["derive_s"] += time.perf_counter() - t

    async def get_text(self, url: str, *, headers: dict[str, str] | None = None,
                       max_bytes: int = MAX_TEXT_BYTES) -> str:
        """GET a page as text (HTML). Rate limited and retried like any GET; never cached. The body is streamed and
        reading stops after `max_bytes`, so the result is at most that long (a truncated page, never an error).
        A caller that must not act on a partial page uses `get_text_capped`."""
        return (await self.get_text_capped(url, headers=headers, max_bytes=max_bytes))[0]

    async def get_text_capped(self, url: str, *, headers: dict[str, str] | None = None,
                              max_bytes: int = MAX_TEXT_BYTES) -> tuple[str, bool]:
        """(text, truncated) for a GET as in `get_text`; truncated is True only when the body was longer than
        `max_bytes` (and was cut there)."""
        resp = await self._send("GET", url, headers=headers, retry=True, max_bytes=max_bytes)
        return resp.text, bool(resp.extensions.get("jam_truncated"))

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
        t = time.perf_counter()
        entry = self._load(key) if key else None
        ttl = self.ttl_s if ttl_s is None else ttl_s
        if entry and self.clock() - entry["at"] < ttl:
            self.stats["cache_hits"] += 1
            self.timings["cache_s"] += time.perf_counter() - t
            if entry.get("status") == 404:   # negative entry: a dead board is not re-requested inside the TTL
                raise HttpError(404, url)
            t = time.perf_counter()
            try:
                return json.loads(entry["body"])
            finally:
                self.timings["cache_s"] += time.perf_counter() - t
        self.timings["cache_s"] += time.perf_counter() - t
        if entry and entry.get("status") == 404:
            entry = None   # stale negative entry: refetch, and never serve it as stale-if-error
        hdrs = dict(headers or {})
        if entry and entry.get("etag"):
            hdrs["If-None-Match"] = entry["etag"]
        try:
            resp = await self._send(method, url, params=params, headers=hdrs, json_body=json_body, retry=True)
        except (HttpError, httpx.TransportError) as e:
            if entry and (isinstance(e, httpx.TransportError) or e.status >= 500):
                self.stats["stale_served"] += 1
                return json.loads(entry["body"])
            if key and isinstance(e, HttpError) and e.status == 404:
                self._store(key, {"at": self.clock(), "status": 404, "body": ""})
            raise
        if resp.status_code == 304 and entry:
            self.stats["revalidated"] += 1
            entry["at"] = self.clock()
            self._store(key, entry)
            return json.loads(entry["body"])
        if key:
            self._store(key, {"at": self.clock(), "etag": resp.headers.get("etag"), "body": resp.text})
        return self._decode(resp)

    @staticmethod
    def _digest(body: str) -> str:
        return hashlib.sha1(body.encode("utf-8", "surrogatepass")).hexdigest()

    async def _send(self, method: str, url: str, *, retry: bool, params=None, headers=None, json_body=None,
                    data=None, files=None, auth=None, max_bytes: int | None = None) -> httpx.Response:
        attempts = (self.retries if retry else 0) + 1
        for i in range(attempts):
            try:
                if self.limiter:
                    async with await self.limiter(url):
                        resp = await self._timed_request(method, url, params, headers, json_body, data, files, auth,
                                                         max_bytes)
                else:
                    resp = await self._timed_request(method, url, params, headers, json_body, data, files, auth,
                                                     max_bytes)
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

    async def _timed_request(self, *a: Any) -> httpx.Response:
        t = time.perf_counter()
        try:
            return await self._request(*a)
        finally:
            self.timings["network_s"] += time.perf_counter() - t

    async def _request(self, method, url, params, headers, json_body, data, files, auth,
                       max_bytes: int | None = None) -> httpx.Response:
        if max_bytes is None:
            return await self._http().request(method, url, params=params, headers=headers, json=json_body,
                                              data=data, files=files, auth=auth)
        client = self._http()
        req = client.build_request(method, url, params=params, headers=headers, json=json_body, data=data,
                                   files=files)
        resp = await client.send(req, auth=auth, stream=True)
        try:   # stop reading at the cap; the connection is closed rather than drained
            body = bytearray()
            async for chunk in resp.aiter_bytes():
                body += chunk
                if len(body) > max_bytes:   # one byte past the cap, so truncation is detectable
                    break
        finally:
            await resp.aclose()
        hdrs = [(k, v) for k, v in resp.headers.multi_items()
                if k.lower() not in ("content-encoding", "content-length", "transfer-encoding")]   # body is decoded
        return httpx.Response(resp.status_code, headers=hdrs, content=bytes(body[:max_bytes]), request=req,
                              history=resp.history,
                              extensions={**resp.extensions, "jam_truncated": len(body) > max_bytes})

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
            entry = json.loads(self._path(key).read_text(encoding="utf-8"))
        except (OSError, ValueError):
            self._meta.pop(key, None)
            return None
        self._remember(key, entry)
        return entry

    def _remember(self, key: str, entry: dict) -> None:
        """Track the entry's timestamp and content digest in memory (for `get_derived`)."""
        digest = entry.get("digest") or self._digest(str(entry.get("body", "")))
        entry["digest"] = digest
        self._meta[key] = {"at": entry.get("at", 0), "digest": digest}

    def _store(self, key: str | None, entry: dict) -> None:
        if not key or not self.cache_dir:
            return
        self._remember(key, entry)
        try:
            self.cache_dir.mkdir(parents=True, exist_ok=True)
            tmp = self._path(key).with_suffix(f".{os.getpid()}.tmp")
            tmp.write_text(json.dumps(entry), encoding="utf-8")
            tmp.replace(self._path(key))
        except OSError:
            pass  # cache is best-effort

    def _prune_once(self) -> None:
        if self._pruned or not self.cache_dir:
            return
        self._pruned = True
        cutoff = self.clock() - self.prune_days * 86400
        try:   # file mtime == the entry's last store (fetch or revalidation); stat only, never parse (ADR-0043)
            for p in self.cache_dir.glob("*.json"):
                try:
                    if p.stat().st_mtime < cutoff:
                        p.unlink()
                except OSError:
                    pass
        except OSError:
            pass

import os

import httpx
import pytest
import respx

from job_apply_mcp.core.concurrency import HostLimiter
from job_apply_mcp.platforms import HttpClient, HttpError


@respx.mock
async def test_ttl_cache_then_etag_revalidation(tmp_path):
    now = [1000.0]
    c = HttpClient(cache_dir=tmp_path, ttl_s=60, backoff_s=0, clock=lambda: now[0])
    route = respx.get("https://x.test/a").mock(return_value=httpx.Response(200, json={"v": 1}, headers={"etag": '"e1"'}))
    assert await c.get_json("https://x.test/a") == {"v": 1}
    assert await c.get_json("https://x.test/a") == {"v": 1} and route.call_count == 1  # fresh: no request
    now[0] += 120
    route.mock(return_value=httpx.Response(304))
    assert await c.get_json("https://x.test/a") == {"v": 1}
    assert route.calls.last.request.headers["if-none-match"] == '"e1"'
    assert await c.get_json("https://x.test/a") == {"v": 1} and route.call_count == 2  # timestamp refreshed
    assert c.stats["revalidated"] == 1
    # a second client process sees the same disk cache
    c2 = HttpClient(cache_dir=tmp_path, ttl_s=60, clock=lambda: now[0])
    assert await c2.get_json("https://x.test/a") == {"v": 1} and route.call_count == 2
    await c.aclose(); await c2.aclose()


@respx.mock
async def test_params_and_post_body_are_part_of_cache_key(http):
    r = respx.get("https://x.test/l").mock(side_effect=lambda req: httpx.Response(200, json={"q": req.url.params["q"]}))
    assert (await http.get_json("https://x.test/l", params={"q": "a"}))["q"] == "a"
    assert (await http.get_json("https://x.test/l", params={"q": "b"}))["q"] == "b" and r.call_count == 2
    p = respx.post("https://x.test/s").mock(side_effect=lambda req: httpx.Response(200, json={"n": req.content.decode()}))
    await http.post_json("https://x.test/s", json_body={"o": 1})
    await http.post_json("https://x.test/s", json_body={"o": 1})
    await http.post_json("https://x.test/s", json_body={"o": 2})
    assert p.call_count == 2


@respx.mock
async def test_retries_stale_if_error_and_no_retry_for_forms(tmp_path):
    now = [0.0]
    c = HttpClient(cache_dir=tmp_path, ttl_s=10, retries=2, backoff_s=0, clock=lambda: now[0])
    route = respx.get("https://x.test/r").mock(side_effect=[httpx.Response(503), httpx.Response(429), httpx.Response(200, json=[1])])
    assert await c.get_json("https://x.test/r") == [1] and route.call_count == 3
    now[0] = 100
    route.mock(return_value=httpx.Response(500))
    assert await c.get_json("https://x.test/r") == [1] and c.stats["stale_served"] == 1  # stale-if-error
    respx.get("https://x.test/other").respond(500)
    with pytest.raises(HttpError) as ei:
        await c.get_json("https://x.test/other")
    assert ei.value.status == 500
    post = respx.post("https://x.test/f").mock(return_value=httpx.Response(503))
    with pytest.raises(HttpError):
        await c.post_form("https://x.test/f", data={"a": "1"})
    assert post.call_count == 1  # never retry a non-idempotent submission
    await c.aclose()


@respx.mock
async def test_errors_hide_query_string_and_404_is_negatively_cached(tmp_path):
    now = [1000.0]
    c = HttpClient(cache_dir=tmp_path, ttl_s=60, backoff_s=0, clock=lambda: now[0])
    r = respx.get("https://x.test/k").respond(404)
    for _ in range(2):   # ADR-0043: a dead board costs one request per TTL, not one per search
        with pytest.raises(HttpError) as ei:
            await c.get_json("https://x.test/k", params={"key": "SECRET"})
        assert "SECRET" not in str(ei.value) and ei.value.status == 404
    assert r.call_count == 1
    now[0] += 120
    r.respond(json={"back": True})   # the board came back: the stale negative entry is refetched, never served
    assert await c.get_json("https://x.test/k", params={"key": "SECRET"}) == {"back": True} and r.call_count == 2
    await c.aclose()


@respx.mock
async def test_limiter_is_used_and_prune(tmp_path):
    seen = []

    class L(HostLimiter):
        async def __call__(self, url):
            seen.append(url)
            return await super().__call__(url)

    now = [10 * 86400.0]
    (tmp_path / "old.json").write_text('{"at": 0, "body": "1"}')
    os.utime(tmp_path / "old.json", (0, 0))   # pruning reads the file mtime, never the body (ADR-0043)
    c = HttpClient(cache_dir=tmp_path, limiter=L(per_host=1), prune_days=7, clock=lambda: now[0])
    respx.get("https://x.test/z").respond(json=1)
    await c.get_json("https://x.test/z")
    assert seen == ["https://x.test/z"] and not (tmp_path / "old.json").exists()
    await c.aclose()

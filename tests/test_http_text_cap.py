"""PR #117 review: HttpClient.get_text streams the body and stops reading at a size cap, keeping the GET retries,
rate limit and decoding. Fictional hosts; all HTTP is mocked."""
import gzip

import httpx
import pytest
import respx

from job_apply_mcp.core.concurrency import HostLimiter
from job_apply_mcp.platforms import HttpClient, HttpError
from job_apply_mcp.platforms.http import MAX_TEXT_BYTES


class Endless(httpx.AsyncByteStream):
    """A body that never ends; counts how many chunks were pulled."""
    def __init__(self):
        self.pulled = 0

    async def __aiter__(self):
        while True:
            self.pulled += 1
            yield b"x" * 65536

    async def aclose(self):
        pass


@respx.mock
async def test_endless_body_stops_at_the_cap(http):
    stream = Endless()
    respx.get("https://big.test/page").mock(return_value=httpx.Response(200, stream=stream))
    text = await http.get_text("https://big.test/page", max_bytes=300_000)
    assert len(text) == 300_000 and stream.pulled <= 300_000 // 65536 + 1


@respx.mock
async def test_default_cap_is_two_megabytes(http):
    respx.get("https://big.test/page").mock(return_value=httpx.Response(200, stream=Endless()))
    assert MAX_TEXT_BYTES == 2_000_000
    assert len(await http.get_text("https://big.test/page")) == MAX_TEXT_BYTES


@respx.mock
async def test_small_body_charset_and_gzip_are_unchanged(http):
    respx.get("https://x.test/a").respond(text="<p>café</p>", headers={"content-type": "text/html; charset=utf-8"})
    assert await http.get_text("https://x.test/a") == "<p>café</p>"
    respx.get("https://x.test/z").respond(content=gzip.compress(b"<p>zipped</p>"),
                                          headers={"content-encoding": "gzip", "content-type": "text/html"})
    assert await http.get_text("https://x.test/z") == "<p>zipped</p>"


@respx.mock
async def test_retries_errors_and_rate_limit_still_apply():
    calls = []
    lim = HostLimiter(per_host=1, min_interval_s=0)

    async def spy(url):   # every attempt goes through the host limiter
        calls.append(url)
        return await lim(url)
    c = HttpClient(backoff_s=0, retries=2, limiter=spy)   # type: ignore[arg-type]
    r = respx.get("https://x.test/r").mock(side_effect=[httpx.Response(503), httpx.Response(200, text="ok")])
    assert await c.get_text("https://x.test/r") == "ok" and r.call_count == 2 and len(calls) == 2
    respx.get("https://x.test/gone").respond(404, text="nope")
    with pytest.raises(HttpError) as e:
        await c.get_text("https://x.test/gone")
    assert e.value.status == 404 and e.value.body == "nope"
    await c.aclose()


@respx.mock
async def test_json_callers_are_not_capped(http):
    respx.get("https://x.test/j").respond(json={"v": "y" * (MAX_TEXT_BYTES + 10)})
    assert len((await http.get_json("https://x.test/j", cache=False))["v"]) == MAX_TEXT_BYTES + 10

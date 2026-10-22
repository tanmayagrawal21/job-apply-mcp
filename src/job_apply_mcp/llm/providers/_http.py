"""Shared httpx helpers for providers."""
from __future__ import annotations

import httpx

from ..base import ProviderError


def raise_for(resp: httpx.Response) -> None:
    if resp.status_code < 400:
        return
    retry = resp.status_code in (408, 409, 425, 429) or resp.status_code >= 500
    raise ProviderError(f"HTTP {resp.status_code}: {resp.text[:200]}", retryable=retry)


async def request(client: httpx.AsyncClient, method: str, url: str, **kw) -> httpx.Response:
    try:
        resp = await client.request(method, url, **kw)
    except (httpx.TimeoutException, httpx.TransportError) as e:
        raise ProviderError(f"{type(e).__name__}: {e}", retryable=True) from e
    raise_for(resp)
    return resp

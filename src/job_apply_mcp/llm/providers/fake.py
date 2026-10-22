"""Scriptable provider for tests. `responses` is a list consumed in order (last one repeats), or a callable."""
from __future__ import annotations

import asyncio
from collections.abc import Callable
from typing import Any

from ...core.registry import llm_providers
from ..base import Completion, ProviderError, estimate_tokens


class FakeProvider:
    def __init__(self, name: str = "fake", responses: list[Any] | Callable[..., Any] | None = None,
                 is_up: bool = True, models: list[str] | None = None, latency_s: float = 0.0,
                 is_cloud: bool = False, **_):
        self.name, self.is_up, self.latency_s, self.is_cloud = name, is_up, latency_s, is_cloud
        self.models = models
        self.responses = responses if responses is not None else ["ok"]
        self.calls: list[dict] = []
        self.active = self.peak = 0

    async def available(self) -> bool:
        return self.is_up

    async def list_models(self) -> list[str]:
        return list(self.models or [])

    async def has_model(self, model: str) -> bool:
        return self.models is None or model in self.models

    async def complete_ex(self, prompt: str, *, model: str, system: str | None = None, max_tokens: int = 512,
                          temperature: float = 0.2, json_schema: dict | None = None, **_) -> Completion:
        self.calls.append({"prompt": prompt, "model": model, "system": system, "max_tokens": max_tokens,
                           "json_schema": json_schema})
        self.active += 1
        self.peak = max(self.peak, self.active)
        try:
            if self.latency_s:
                await asyncio.sleep(self.latency_s)
            if callable(self.responses):
                r = self.responses(prompt, model, len(self.calls))
            else:
                r = self.responses[min(len(self.calls) - 1, len(self.responses) - 1)]
            if isinstance(r, Exception):
                raise r
            if isinstance(r, str) and r.startswith("!retry"):
                raise ProviderError("flaky", retryable=True)
            return Completion(r, estimate_tokens(prompt), estimate_tokens(r))
        finally:
            self.active -= 1

    async def complete(self, prompt: str, *, model: str, **kw) -> str:
        return (await self.complete_ex(prompt, model=model, **kw)).text


@llm_providers.register("fake")
def _make(**cfg) -> FakeProvider:
    return FakeProvider(**cfg)

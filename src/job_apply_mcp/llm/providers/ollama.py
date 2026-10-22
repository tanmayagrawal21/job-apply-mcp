"""Ollama native API: /api/chat for generation, /api/tags for availability and model listing."""
from __future__ import annotations

import time

import httpx

from ...core.registry import llm_providers
from ..base import Completion, ProviderError, strip_think
from ._http import request


class OllamaProvider:
    name = "ollama"
    supports_options = True   # accepts think / num_ctx
    supports_timeout = True   # accepts a per-call timeout_s (ADR-0041)
    is_cloud = False

    def __init__(self, base_url: str = "http://127.0.0.1:11434", timeout_s: float = 120.0,
                 probe_timeout_s: float = 2.0, keep_alive: str | None = "30m", **_):
        self.base_url = base_url.rstrip("/")
        self.timeout_s, self.probe_timeout_s, self.keep_alive = timeout_s, probe_timeout_s, keep_alive
        self._models: list[str] = []
        self._probed_at = 0.0

    async def list_models(self) -> list[str]:
        try:
            async with httpx.AsyncClient(timeout=self.probe_timeout_s) as c:
                r = await request(c, "GET", f"{self.base_url}/api/tags")
            self._models = [m["name"] for m in r.json().get("models", [])]
        except (ProviderError, ValueError, KeyError):
            self._models = []
        self._probed_at = time.monotonic()
        return list(self._models)

    async def available(self) -> bool:
        return bool(await self.list_models())

    async def has_model(self, model: str) -> bool:
        names = self._models or await self.list_models()
        return model in names or (":" not in model and f"{model}:latest" in names)

    async def complete_ex(self, prompt: str, *, model: str, system: str | None = None, max_tokens: int = 512,
                          temperature: float = 0.2, json_schema: dict | None = None, think: bool = False,
                          num_ctx: int | None = None, timeout_s: float | None = None) -> Completion:
        msgs = ([{"role": "system", "content": system}] if system else []) + [{"role": "user", "content": prompt}]
        body: dict = {"model": model, "messages": msgs, "stream": False, "think": think,
                      "options": {"num_predict": max_tokens, "temperature": temperature}}
        if num_ctx:
            body["options"]["num_ctx"] = num_ctx
        if json_schema is not None:
            body["format"] = json_schema if json_schema else "json"
        if self.keep_alive:
            body["keep_alive"] = self.keep_alive
        async with httpx.AsyncClient(timeout=timeout_s or self.timeout_s) as c:
            r = await request(c, "POST", f"{self.base_url}/api/chat", json=body)
        j = r.json()
        text = strip_think((j.get("message") or {}).get("content", ""))
        return Completion(text, j.get("prompt_eval_count", 0), j.get("eval_count", 0),
                          j.get("eval_duration", 0) / 1e6)

    async def warm(self, model: str, num_ctx: int | None = None) -> bool:
        """Preload a model and keep it resident (empty generate request with keep_alive)."""
        body: dict = {"model": model, "prompt": "", "stream": False, "keep_alive": self.keep_alive or "30m"}
        if num_ctx:
            body["options"] = {"num_ctx": num_ctx}
        try:
            async with httpx.AsyncClient(timeout=self.timeout_s) as c:
                await request(c, "POST", f"{self.base_url}/api/generate", json=body)
            return True
        except ProviderError:
            return False

    async def complete(self, prompt: str, *, model: str, **kw) -> str:
        return (await self.complete_ex(prompt, model=model, **kw)).text


@llm_providers.register("ollama")
def _make(**cfg) -> OllamaProvider:
    return OllamaProvider(**cfg)

"""Any OpenAI-compatible /chat/completions server: mlx_lm.server, LM Studio, vLLM, llama.cpp, OpenAI, OpenRouter."""
from __future__ import annotations

import json
import os
from urllib.parse import urlparse

import httpx

from ...core.registry import llm_providers
from ..base import Completion, ProviderError, strip_think
from ._http import request

_LOCAL_HOSTS = {"localhost", "127.0.0.1", "::1", "0.0.0.0"}


class OpenAICompatProvider:
    name = "openai_compat"
    supports_options = True

    def __init__(self, base_url: str = "", api_key_env: str = "OPENAI_API_KEY", timeout_s: float = 120.0,
                 probe_timeout_s: float = 3.0, local: bool | None = None, extra_headers: dict | None = None, **_):
        self.base_url = base_url.rstrip("/")
        self.api_key_env, self.timeout_s, self.probe_timeout_s = api_key_env, timeout_s, probe_timeout_s
        self.extra_headers = extra_headers or {}
        host = urlparse(self.base_url).hostname or ""
        self.is_cloud = (not (host in _LOCAL_HOSTS or host.endswith(".local"))) if local is None else not local

    def _headers(self) -> dict:
        key = os.environ.get(self.api_key_env, "") if self.api_key_env else ""
        return {**({"Authorization": f"Bearer {key}"} if key else {}), **self.extra_headers}

    async def list_models(self) -> list[str]:
        if not self.base_url:
            return []
        try:
            async with httpx.AsyncClient(timeout=self.probe_timeout_s) as c:
                r = await request(c, "GET", f"{self.base_url}/models", headers=self._headers())
            return [m["id"] for m in r.json().get("data", [])]
        except (ProviderError, ValueError, KeyError):
            return []

    async def available(self) -> bool:
        if not self.base_url:
            return False
        if self.is_cloud and self.api_key_env and not os.environ.get(self.api_key_env):
            return False
        return bool(await self.list_models()) if not self.is_cloud else True

    async def complete_ex(self, prompt: str, *, model: str, system: str | None = None, max_tokens: int = 512,
                          temperature: float = 0.2, json_schema: dict | None = None, think: bool = False,
                          num_ctx: int | None = None) -> Completion:
        sys_txt = system
        if json_schema:
            hint = "Reply with only JSON matching this schema: " + json.dumps(json_schema, separators=(",", ":"))
            sys_txt = f"{system}\n{hint}" if system else hint
        if not think and "qwen3" in model.lower():
            prompt = f"{prompt} /no_think"   # qwen3 soft switch for servers without a think flag
        msgs = ([{"role": "system", "content": sys_txt}] if sys_txt else []) + [{"role": "user", "content": prompt}]
        body: dict = {"model": model, "messages": msgs, "max_tokens": max_tokens, "temperature": temperature}
        url = f"{self.base_url}/chat/completions"
        async with httpx.AsyncClient(timeout=self.timeout_s) as c:
            if json_schema:
                strict = {"type": "json_schema", "json_schema": {"name": "out", "schema": json_schema}}
                try:
                    r = await request(c, "POST", url, headers=self._headers(),
                                      json={**body, "response_format": strict})
                except ProviderError as e:
                    if e.retryable:
                        raise
                    # server rejected json_schema; fall back to plain prompt-level JSON instruction
                    r = await request(c, "POST", url, headers=self._headers(), json=body)
            else:
                r = await request(c, "POST", url, headers=self._headers(), json=body)
        j = r.json()
        try:
            text = j["choices"][0]["message"]["content"] or ""
        except (KeyError, IndexError, TypeError) as e:
            raise ProviderError(f"malformed response: {str(j)[:200]}") from e
        u = j.get("usage") or {}
        return Completion(strip_think(text), u.get("prompt_tokens", 0), u.get("completion_tokens", 0))

    async def complete(self, prompt: str, *, model: str, **kw) -> str:
        return (await self.complete_ex(prompt, model=model, **kw)).text


@llm_providers.register("openai_compat")
def _make(**cfg) -> OpenAICompatProvider:
    return OpenAICompatProvider(**cfg)

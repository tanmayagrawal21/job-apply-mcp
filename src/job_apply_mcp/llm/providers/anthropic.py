"""Anthropic Messages API over httpx (no SDK dependency). Key is read from the env var named in config."""
from __future__ import annotations

import json
import os

import httpx

from ...core.registry import llm_providers
from ..base import Completion
from ._http import request


class AnthropicProvider:
    name = "anthropic"
    is_cloud = True

    def __init__(self, api_key_env: str = "ANTHROPIC_API_KEY", base_url: str = "https://api.anthropic.com",
                 timeout_s: float = 120.0, version: str = "2023-06-01", **_):
        self.api_key_env, self.base_url = api_key_env, base_url.rstrip("/")
        self.timeout_s, self.version = timeout_s, version

    async def available(self) -> bool:
        return bool(os.environ.get(self.api_key_env, ""))

    async def list_models(self) -> list[str]:
        return []

    async def complete_ex(self, prompt: str, *, model: str, system: str | None = None, max_tokens: int = 512,
                          temperature: float = 0.2, json_schema: dict | None = None) -> Completion:
        sys_txt = system
        if json_schema:
            hint = "Reply with only JSON matching this schema: " + json.dumps(json_schema, separators=(",", ":"))
            sys_txt = f"{system}\n{hint}" if system else hint
        body: dict = {"model": model, "max_tokens": max_tokens, "temperature": temperature,
                      "messages": [{"role": "user", "content": prompt}]}
        if sys_txt:
            body["system"] = sys_txt
        headers = {"x-api-key": os.environ.get(self.api_key_env, ""), "anthropic-version": self.version}
        async with httpx.AsyncClient(timeout=self.timeout_s) as c:
            r = await request(c, "POST", f"{self.base_url}/v1/messages", headers=headers, json=body)
        j = r.json()
        text = "".join(b.get("text", "") for b in j.get("content", []) if b.get("type") == "text")
        u = j.get("usage") or {}
        return Completion(text, u.get("input_tokens", 0), u.get("output_tokens", 0))

    async def complete(self, prompt: str, *, model: str, **kw) -> str:
        return (await self.complete_ex(prompt, model=model, **kw)).text


@llm_providers.register("anthropic")
def _make(**cfg) -> AnthropicProvider:
    return AnthropicProvider(**cfg)

"""Optional LiteLLM bridge (extra `litellm`, pinned >=1.83.0 because 1.82.7/1.82.8 were malicious; ADR-0006).

The core router never needs it: it speaks OpenAI-compatible HTTP directly. This exists so users who already
rely on LiteLLM's many provider adapters can plug it in. Version is checked at runtime and older ones refused.
"""
from __future__ import annotations

import importlib.util
import json
from importlib import metadata

from ...core.registry import llm_providers
from ..base import Completion, ProviderError, estimate_tokens

MIN_VERSION = (1, 83, 0)


def _version() -> tuple[int, ...] | None:
    try:
        v = metadata.version("litellm")
    except metadata.PackageNotFoundError:
        return None
    parts = []
    for p in v.split(".")[:3]:
        digits = "".join(ch for ch in p if ch.isdigit())
        parts.append(int(digits or 0))
    return tuple(parts)


class LiteLLMProvider:
    name = "litellm"
    is_cloud = True

    def __init__(self, timeout_s: float = 120.0, **_):
        self.timeout_s = timeout_s

    async def available(self) -> bool:
        v = _version()
        return importlib.util.find_spec("litellm") is not None and v is not None and v >= MIN_VERSION

    async def list_models(self) -> list[str]:
        return []

    async def complete_ex(self, prompt: str, *, model: str, system: str | None = None, max_tokens: int = 512,
                          temperature: float = 0.2, json_schema: dict | None = None) -> Completion:
        if not await self.available():
            raise ProviderError("litellm >=1.83.0 not installed (extra 'litellm')")
        import litellm  # lazy

        sys_txt = system
        if json_schema:
            hint = "Reply with only JSON matching: " + json.dumps(json_schema, separators=(",", ":"))
            sys_txt = f"{system}\n{hint}" if system else hint
        msgs = ([{"role": "system", "content": sys_txt}] if sys_txt else []) + [{"role": "user", "content": prompt}]
        try:
            r = await litellm.acompletion(model=model, messages=msgs, max_tokens=max_tokens,
                                          temperature=temperature, timeout=self.timeout_s)
        except Exception as e:
            raise ProviderError(f"litellm: {e}", retryable=True) from e
        text = r.choices[0].message.content or ""
        u = getattr(r, "usage", None)
        return Completion(text, getattr(u, "prompt_tokens", estimate_tokens(prompt)),
                          getattr(u, "completion_tokens", estimate_tokens(text)))

    async def complete(self, prompt: str, *, model: str, **kw) -> str:
        return (await self.complete_ex(prompt, model=model, **kw)).text


@llm_providers.register("litellm")
def _make(**cfg) -> LiteLLMProvider:
    return LiteLLMProvider(**cfg)

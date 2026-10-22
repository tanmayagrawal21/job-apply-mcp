"""Hugging Face transformers pipeline (optional extra `hf`). Lazily imported, loaded once per model, cached."""
from __future__ import annotations

import asyncio
import importlib.util
import json
from typing import Any

from ...core.registry import llm_providers
from ..base import Completion, ProviderError, estimate_tokens, strip_think


class HFLocalProvider:
    name = "hf_local"
    is_cloud = False

    def __init__(self, device: str = "auto", **_):
        self.device = device
        self._pipes: dict[str, Any] = {}
        self._lock = asyncio.Lock()

    async def available(self) -> bool:
        return importlib.util.find_spec("transformers") is not None

    async def list_models(self) -> list[str]:
        return list(self._pipes)

    def _load(self, model: str):
        from transformers import pipeline  # lazy: optional dependency

        return pipeline("text-generation", model=model, device=self._resolve_device())

    def _resolve_device(self) -> str:
        """'auto' picks cuda, then Apple mps, then cpu (no macOS assumption, ADR-0020)."""
        if self.device != "auto":
            return self.device
        try:
            import torch  # installed with transformers

            if torch.cuda.is_available():
                return "cuda"
            if getattr(torch.backends, "mps", None) and torch.backends.mps.is_available():
                return "mps"
        except Exception:  # noqa: BLE001, S110 - detection must never break loading
            pass
        return "cpu"

    async def complete_ex(self, prompt: str, *, model: str, system: str | None = None, max_tokens: int = 512,
                          temperature: float = 0.2, json_schema: dict | None = None) -> Completion:
        if not await self.available():
            raise ProviderError("transformers not installed; install the 'hf' extra")
        async with self._lock:
            if model not in self._pipes:
                self._pipes[model] = await asyncio.to_thread(self._load, model)
        pipe = self._pipes[model]
        sys_txt = system or ""
        if json_schema:
            sys_txt += "\nReply with only JSON matching: " + json.dumps(json_schema, separators=(",", ":"))
        msgs = ([{"role": "system", "content": sys_txt.strip()}] if sys_txt.strip() else []) + [
            {"role": "user", "content": prompt}]

        def run() -> str:
            out = pipe(msgs, max_new_tokens=max_tokens, do_sample=temperature > 0,
                       **({"temperature": temperature} if temperature > 0 else {}))
            gen = out[0]["generated_text"]
            return gen[-1]["content"] if isinstance(gen, list) else str(gen)

        text = strip_think(await asyncio.to_thread(run))
        return Completion(text, estimate_tokens(prompt), estimate_tokens(text))

    async def complete(self, prompt: str, *, model: str, **kw) -> str:
        return (await self.complete_ex(prompt, model=model, **kw)).text


@llm_providers.register("hf_local")
def _make(**cfg) -> HFLocalProvider:
    return HFLocalProvider(**cfg)

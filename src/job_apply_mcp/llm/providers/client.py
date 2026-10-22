"""The calling MCP agent is the model. No generation happens here.

If a sampling function is available (MCP `sampling/createMessage`, when the client advertises it) the
text is requested through it. Otherwise `ClientTaskRequired` is raised and the router returns the task
as structured data (`client_task`) for the calling agent to write.
"""
from __future__ import annotations

import contextvars
from typing import Any

from ...core.registry import llm_providers
from ..base import ClientTask, ClientTaskRequired, Completion, Sampler, estimate_tokens

# Set per request by the server wiring: `current_sampler.set(sampler_from_context(ctx))`.
current_sampler: contextvars.ContextVar[Sampler | None] = contextvars.ContextVar("jam_sampler", default=None)


def sampler_from_context(ctx: Any) -> Sampler | None:
    """Build a sampler from a FastMCP Context if the connected client advertises sampling, else None."""
    try:
        session = ctx.session
        caps = session.client_params.capabilities
        if getattr(caps, "sampling", None) is None:
            return None
    except AttributeError:
        return None

    async def sample(prompt: str, *, system: str | None = None, max_tokens: int = 512, **_) -> str:
        from mcp.types import SamplingMessage, TextContent  # lazy

        res = await session.create_message(
            messages=[SamplingMessage(role="user", content=TextContent(type="text", text=prompt))],
            max_tokens=max_tokens, system_prompt=system)
        c = res.content
        return getattr(c, "text", "") or ""

    return sample


class ClientProvider:
    name = "client"
    is_cloud = True  # the calling agent's model is a remote model unless the user says otherwise

    def __init__(self, **_):
        pass

    async def available(self) -> bool:
        return True

    async def list_models(self) -> list[str]:
        return ["client-default"]

    async def complete_ex(self, prompt: str, *, model: str = "client-default", system: str | None = None,
                          max_tokens: int = 512, temperature: float = 0.2,
                          json_schema: dict | None = None, key: str = "") -> Completion:
        sampler = current_sampler.get()
        if sampler is not None:
            try:
                text = await sampler(prompt, system=system, max_tokens=max_tokens)
                return Completion(text, estimate_tokens(prompt), estimate_tokens(text))
            except Exception:  # noqa: BLE001, S110 - sampling is best effort; hand the task back instead
                pass
        raise ClientTaskRequired(ClientTask(field_key=key, instruction=prompt, system=system,
                                            max_tokens=max_tokens, json_schema=json_schema))

    async def complete(self, prompt: str, *, model: str = "client-default", **kw) -> str:
        return (await self.complete_ex(prompt, model=model, **kw)).text




@llm_providers.register("client")
def _make(**cfg) -> ClientProvider:
    return ClientProvider(**cfg)

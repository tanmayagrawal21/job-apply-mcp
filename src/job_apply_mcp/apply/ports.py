"""Narrow Protocols for the collaborators the apply engine needs from other components.

The engine codes only against these (plus core/interfaces.py), so profile, router, gates and store
implementations can land independently and be swapped or faked in tests.
"""
from __future__ import annotations

import contextvars
from collections.abc import Mapping
from typing import Any, Protocol, runtime_checkable

from ..core.models import ApplicationForm, GateResult, Posting


class ComponentMissing(RuntimeError):
    """A component the engine needs has not been wired in (or is not installed)."""


# Per-call final click (ADR-0060): submit_application(final_click="human") sets this for the call's task. TIGHTEN-ONLY:
# only "human" is ever stored here; a per-call "agent" never overrides a configured "human".
FINAL_CLICK: contextvars.ContextVar[str | None] = contextvars.ContextVar("jam_final_click", default=None)


def final_click(configured: str) -> str:
    """Effective final click: "human" if the config OR this call asks for it, else the configured value."""
    return "human" if configured == "human" or FINAL_CLICK.get() == "human" else configured


@runtime_checkable
class ProfileLike(Protocol):
    resume_path: str | None
    cover_letter_path: str | None

    def lookup(self, key: str) -> Any | None:
        """Standard answer for a canonical key ('first_name', 'email', 'work_authorized', 'gender', ...)
        or a normalized question label. Return None when unknown. The sentinel 'decline' means the
        candidate prefers not to answer demographic questions."""

    def facts(self, topic: str = "", limit: int = 12) -> list[str]:
        """Short factual statements (roles, projects, skills) relevant to `topic`, most relevant first."""


@runtime_checkable
class RouterLike(Protocol):
    async def complete(self, prompt: str, *, task_class: str = "short", system: str | None = None,
                       max_tokens: int = 256, json_schema: dict | None = None) -> str:
        """Route to the cheapest adequate model. task_class: 'short' | 'narrative' | 'review'."""


@runtime_checkable
class GatesLike(Protocol):
    async def evaluate(self, posting: Posting, form: ApplicationForm) -> GateResult: ...


AdapterResolver = Any  # Callable[[str], PlatformAdapter]; kept loose so tests can pass lambdas
Answers = Mapping[str, Any]


@runtime_checkable
class ControlLike(Protocol):
    """Optional control layer (ADR-0014): `check("submit", ctx)` may block or hand off to a human."""

    async def check(self, action: str, ctx: dict[str, Any]) -> Any: ...

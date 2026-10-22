"""Extension points. Each is a Protocol so components can be built, tested and swapped independently.

Implementations register themselves in a registry (see core/registry.py) keyed by name, so adding a
platform or LLM provider never requires editing the server.
"""
from __future__ import annotations

from collections.abc import AsyncIterator
from typing import Any, Protocol, runtime_checkable

from .models import ApplicationForm, Posting, SubmitResult


@runtime_checkable
class PlatformAdapter(Protocol):
    """One ATS / job platform (Greenhouse, Lever, Ashby, Workday, ...)."""

    name: str

    def matches(self, url: str) -> bool:
        """True if this adapter can handle the given posting or board URL."""

    async def list_postings(self, board: str, *, since: str | None = None) -> AsyncIterator[Posting]:
        """Yield postings for one board/company. Must be cheap: no descriptions unless needed."""

    async def get_posting(self, url_or_ref: str, *, with_description: bool = False) -> Posting:
        ...

    async def get_form(self, url_or_ref: str) -> ApplicationForm:
        """Return the application form schema (public API where possible, HTML otherwise)."""

    async def submit(self, form: ApplicationForm, answers: dict[str, Any], files: dict[str, str],
                     *, dry_run: bool = True) -> SubmitResult:
        """Submit via the platform API if credentials allow; otherwise return status='needs_browser'."""


@runtime_checkable
class LLMProvider(Protocol):
    """A text-generation backend (Ollama, HF local, MLX, OpenAI-compatible, Anthropic, client sampling)."""

    name: str

    async def available(self) -> bool: ...

    async def complete(self, prompt: str, *, model: str, system: str | None = None, max_tokens: int = 512,
                       temperature: float = 0.2, json_schema: dict | None = None) -> str: ...


@runtime_checkable
class BrowserDriver(Protocol):
    """Fills and submits a web form. Used when no API submission path exists."""

    name: str

    async def fill_and_submit(self, apply_url: str, form: ApplicationForm, answers: dict[str, Any],
                              files: dict[str, str], *, dry_run: bool = True) -> SubmitResult: ...


@runtime_checkable
class Store(Protocol):
    """Persistence for applications and the duplicate index."""

    async def seen(self, company: str, title: str, *, within_days: int = 180) -> bool: ...

    async def record(self, result: SubmitResult, *, company: str, title: str) -> None: ...

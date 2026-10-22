"""MCP tool: facts_report (resume-first fact precedence and conflicts, ADR-0023)."""
from __future__ import annotations

import asyncio
import json
from collections.abc import Callable
from typing import Any

from ..config import Settings


def build_report(s: Settings) -> dict[str, Any]:
    from ..writing.quality import read_corpus
    from . import load_profile
    from .facts import facts_report, resolve_facts
    from .resume import load_resume
    if not s.paths.resume:
        return {"error": "paths.resume not set"}
    try:
        resume = load_resume(s.paths.resume)
        prof = load_profile(s.paths.profile)
    except (OSError, RuntimeError, ValueError) as e:
        return {"error": str(e)[:200]}
    letters = read_corpus([x for x in [s.writing.style_exemplar, *s.writing.style_corpus] if x])
    return facts_report(resolve_facts(resume, prof, letters))


def register(mcp: Any, get_settings: Callable[[], Settings]) -> None:
    @mcp.tool()
    async def facts_report() -> str:
        """Resume-first fact precedence: counts, profile/resume conflicts, and user-letter facts excluded
        because they conflict with or are outdated relative to the resume (with the reason and the resume's version)."""
        return json.dumps(await asyncio.to_thread(build_report, get_settings()), separators=(",", ":"))

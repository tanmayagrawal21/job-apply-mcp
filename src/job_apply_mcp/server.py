"""MCP entry point. Thin by design: tools validate input, call services, and return compact JSON.

Token rules every tool follows (ADR-0005):
  - default to compact views; `fields=[...]` opts into more
  - paginate with `limit`/`cursor`; never return unbounded lists
  - never echo back large inputs; return refs the caller can pass to later tools
"""
from __future__ import annotations

import json
from typing import Any

from mcp.server.fastmcp import FastMCP

from .config import load_settings
from .services import Services

mcp = FastMCP("job-apply")
_svc: Services | None = None


def svc() -> Services:
    global _svc
    if _svc is None:
        _svc = Services(load_settings())
    return _svc


def _out(obj: Any) -> str:
    s = json.dumps(obj, separators=(",", ":"), default=str)
    budget = svc().settings.response_budget_chars
    return s if len(s) <= budget else s[: budget - 40] + f'..."truncated":{len(s)}}}'


@mcp.tool()
async def search_jobs(query: str = "", boards: list[str] | None = None, platforms: list[str] | None = None,
                      location: str = "", since_days: int = 7, limit: int = 25, cursor: int = 0,
                      apply_policy: bool = True) -> str:
    """Search postings across enabled platforms in parallel. Returns compact rows: ref, co, title, loc, pay, flags."""
    return _out(await svc().search(query=query, boards=boards, platforms=platforms, location=location,
                                   since_days=since_days, limit=limit, cursor=cursor, apply_policy=apply_policy))


@mcp.tool()
async def get_job(ref_or_url: str, fields: list[str] | None = None) -> str:
    """Fetch one posting. Add 'description' to fields to include the (trimmed) description."""
    return _out(await svc().get_job(ref_or_url, fields=fields))


@mcp.tool()
async def get_form(ref_or_url: str) -> str:
    """Return the application form schema (keys, labels, kinds, required, options)."""
    return _out(await svc().get_form(ref_or_url))


@mcp.tool()
async def plan_application(ref_or_url: str) -> str:
    """Run policy gates + duplicate check, fill answers from the profile and local LLMs, and list unresolved fields.
    Unresolved free-text fields come back as `client_tasks` for the calling agent to write."""
    return _out(await svc().plan(ref_or_url))


@mcp.tool()
async def submit_application(ref_or_url: str, answers: dict[str, Any] | None = None,
                             dry_run: bool | None = None, token: str | None = None) -> str:
    """Submit using the best available path (platform API, else browser driver). Honors global dry_run.
    `token` is the approval token from plan_application (required for real submits unless auto_approve)."""
    return _out(await svc().submit(ref_or_url, answers=answers or {}, dry_run=dry_run, token=token))


@mcp.tool()
async def apply_batch(refs: list[str], dry_run: bool | None = None, max_parallel: int | None = None) -> str:
    """Plan + submit many applications concurrently. Returns one compact status row per ref."""
    return _out(await svc().apply_batch(refs, dry_run=dry_run, max_parallel=max_parallel))


@mcp.tool()
async def list_applications(status: str = "", limit: int = 50, cursor: int = 0) -> str:
    """List recorded applications from the local store."""
    return _out(await svc().list_applications(status=status, limit=limit, cursor=cursor))


@mcp.tool()
async def request_feature(description: str, kind: str | None = None) -> str:
    """Propose (and, if a coder backend is configured, implement as a PR for human review) a change to this tool.
    kind: platform_adapter | control | config | new_tool | bug (auto-detected if omitted)."""
    return _out(await svc().request_feature(description, kind))


@mcp.tool()
async def improvement_report(limit: int = 10) -> str:
    """Ranked improvement proposals learned from recent run failures (compact)."""
    return _out(await svc().improvement_report(limit))


@mcp.tool()
async def health() -> str:
    """Report enabled platforms, LLM providers and which local models are reachable."""
    return _out(await svc().health())


from .control.tools import register as _register_control

_register_control(mcp, lambda: svc().settings)


def main() -> None:
    mcp.run()


if __name__ == "__main__":
    main()

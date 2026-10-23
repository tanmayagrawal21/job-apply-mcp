"""MCP tools for application runs (ADR-0045). `register(mcp, get_svc)` is the only wiring server.py needs."""
from __future__ import annotations

import json
from collections.abc import Callable
from typing import Any

from .runner import IntakeRunner


def register(mcp: Any, get_svc: Callable[[], Any]) -> None:
    def _dump(o: Any) -> str:
        return json.dumps(o, separators=(",", ":"), default=str)

    def runner() -> IntakeRunner:
        return IntakeRunner.from_services(get_svc())

    @mcp.tool()
    async def run_applications(links: list[str] | None = None, links_file: str | None = None,
                               sources: list[str] | None = None, max_applications: int | None = None,
                               dry_run: bool | None = None, mail: dict[str, Any] | None = None,
                               messages: list[dict[str, Any]] | None = None,
                               discovery: dict[str, Any] | None = None) -> str:
        """Apply from any mix of sources in one run: explicit `links` (or `links_file`, one URL per line), job links
        the user emailed (read-only mail; `mail` overrides {sender, recipient, query, label, since, since_days};
        sender/recipient default to the user), and discovery (`discovery` = search_jobs arguments). Priority: links,
        then mail, then discovery. `sources` subsets ["links","mail","discovery"]. `max_applications` caps submits
        (default intake.max_applications or run_size feedback). Every URL goes through adapter detection, plan, gates,
        dedupe and submit; unknown hosts are reported as unsupported_site. Prose is deferred: jobs needing unapproved
        prose are skipped, not blocking. With the client mail source pass `messages` [{id, from, to, subject, date,
        body}]. Pending feedback is applied first (see plan_context). Returns the run report."""
        return _dump(await runner().run(links=links, links_file=links_file, sources=sources,
                                        max_applications=max_applications, dry_run=dry_run, mail=mail,
                                        messages=messages, discovery=discovery))

    @mcp.tool()
    async def run_report(run_id: str = "", category: str = "", limit: int = 20) -> str:
        """Report of a run (default: the latest): counts plus rows per category (submitted, dry_run, confirmed,
        handed_off, unknown, skipped, unsupported_site, failed), each with `why`. `category` shows one only."""
        return _dump(await runner().report(run_id, category, limit))

    @mcp.tool()
    async def give_feedback(run_id: str, feedback: str | dict[str, Any], ref: str | None = None) -> str:
        """Feedback on a run (optionally on one `ref` of it), applied at the start of the next run. Structured:
        {"kind":"deny_company"|"deny_title", "pattern"?} (no pattern + ref = that item's company/title),
        {"kind":"answer","pattern","value"}; authorization, demographic and attestation answers (or
        {"kind":"profile_answer","key"|"question","value","scope":"global|company|role_family","scope_value"}) go to
        the profile builder's answer_profile_question, pending until it is installed. {"kind":"salary",
        "salary_floor"|"base_salary"|"default_ask"|"expected"|"delta"|"percent"}: never below the configured floor.
        {"kind":"run_size","value"|"delta"}. Free text is stored and shown in the next run's plan_context."""
        return _dump(await runner().give_feedback(run_id, ref, feedback))

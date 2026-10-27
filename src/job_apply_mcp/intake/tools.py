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
        the user emailed to themselves (read-only mail; sender, recipient and label come from config only; `mail`
        may only narrow with {query, since, since_days}), and discovery (`discovery` = search_jobs arguments). Priority: links,
        then mail, then discovery. `sources` subsets ["links","mail","discovery"]. `max_applications` (whole number
        >= 1; default intake.max_applications or run_size feedback, capped at intake.max_applications_cap) caps the
        eligible postings handled, the same in dry and live mode; skipped and ineligible ones do not count. Every URL
        goes through adapter detection, plan, gates, dedupe and submit. Prose is deferred: jobs needing unapproved
        prose are skipped, not blocking, unless the trusted config enables the essay autodraft (ADR-0057: local
        writer + local strict reviewer; approved essays are filled and labelled `assumed`, rejected ones need the owner). With the client mail source pass `messages` [{id, from, to, subject, date,
        body}]. Pending feedback is applied first (see plan_context). Returns the run report: one status per posting,
        each with `why`: submitted, confirmed, dry_run_ok (a live run would submit it), needs_human (the owner must
        act: unresolved fields, review gate, awaiting approval), quota_blocked (a live quota refused it), skipped
        (policy, duplicate, prose not approved, max_applications reached), unsupported_site, unknown (verify by
        hand), failed. A dry run adds `live_preview` (would_submit, quota_blocked, remaining, warning), always, from the live quotas."""
        return _dump(await runner().run(links=links, links_file=links_file, sources=sources,
                                        max_applications=max_applications, dry_run=dry_run, mail=mail,
                                        messages=messages, discovery=discovery))

    @mcp.tool()
    async def run_report(run_id: str = "", category: str = "", limit: int = 20) -> str:
        """Report of a run (default: the latest): counts plus rows per status (submitted, confirmed, dry_run_ok,
        needs_human, quota_blocked, skipped, unsupported_site, unknown, failed; see run_applications), each with
        `why`. `category` shows one only (the old names handed_off and dry_run still work).
        `auto_attested` lists every attestation ticked under the owner's consent window (ADR-0065) for review."""
        return _dump(await runner().report(run_id, category, limit))

    @mcp.tool()
    async def give_feedback(run_id: str, feedback: str | dict[str, Any], ref: str | None = None) -> str:
        """Feedback on a run (optionally on one `ref` of it), applied at the start of the next run. Structured:
        {"kind":"deny_company"|"deny_title", "pattern"?} (no pattern + ref = that item's company/title),
        {"kind":"answer","pattern","value"} for plain questions only; any authorization, demographic, consent,
        attestation, legal or AI-use question (or {"kind":"profile_answer","key"|"question","value","scope":
        "global|company|role_family","scope_value"}) goes to the profile builder's answer_profile_question.
        {"kind":"salary","salary_floor"|"base_salary"|"default_ask"|"expected"|"delta"|"percent"}: never below the
        configured floor, capped at intake.salary_max_multiple. {"kind":"run_size","value"|"delta"}. Free text is
        shown in the next run's plan_context. Answers given here (and pay changes in human approval mode) apply only
        after the USER confirms them on a terminal: `job-apply-mcp feedback --confirm <id>`."""
        return _dump(await runner().give_feedback(run_id, ref, feedback))

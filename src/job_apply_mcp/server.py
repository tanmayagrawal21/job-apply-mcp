"""MCP entry point. Thin by design: tools validate input, call services, and return compact JSON.

Token rules every tool follows (ADR-0005):
  - default to compact views; `fields=[...]` opts into more
  - paginate with `limit`/`cursor`; never return unbounded lists
  - never echo back large inputs; return refs the caller can pass to later tools
"""
from __future__ import annotations

from typing import Any

from mcp.server.fastmcp import Context, FastMCP
from pydantic import BaseModel, Field

from .config import load_settings
from .core import output as _output
from .grants.otp import assist_code, make_elicitor
from .services import Services

mcp = FastMCP("job-apply")
_raw_tool = mcp.tool


def _guarded_tool(*a: Any, **k: Any):
    """Every registered tool gets the error envelope + schema-safe budgeting (ADR-0005 amendment)."""
    dec = _raw_tool(*a, **k)
    return lambda fn: dec(_output.guarded(fn, lambda: svc().settings.response_budget_chars))


mcp.tool = _guarded_tool  # type: ignore[method-assign]
_output.install(mcp)
_svc: Services | None = None


def svc() -> Services:
    global _svc
    if _svc is None:
        _svc = Services(load_settings())
    return _svc


def _out(obj: Any) -> str:
    return _output.out(obj, svc().settings.response_budget_chars)


@mcp.tool()
async def search_jobs(ctx: Context, query: str = "", boards: list[str] | None = None,
                      platforms: list[str] | None = None, location: str = "", since_days: int = 7, limit: int = 25,
                      cursor: str = "", apply_policy: bool = True) -> str:
    """Search postings across enabled platforms in parallel. Returns compact rows: ref, co, title, loc, pay, flags.
    The first call runs the whole search once; pass the returned `next` as `cursor` for more pages (instant)."""
    async def progress(done: int, total: int) -> None:
        await ctx.report_progress(done, total)

    return _out(await svc().search(query=query, boards=boards, platforms=platforms, location=location,
                                   since_days=since_days, limit=limit, cursor=cursor, apply_policy=apply_policy,
                                   progress=progress))


@mcp.tool()
async def search_status(session_id: str) -> str:
    """Progress of a search_jobs session: status, boards done/total, rows, elapsed. Accepts a session id or cursor."""
    return _out(svc().search_status(session_id))


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
async def draft_answers(ref_or_url: str) -> str:
    """Draft the prose fields a plan left open (writing.mode="deferred", or ones that timed out): local writer +
    reviewer under writing.timeout_s. Returns the same shape as plan_application with a fresh approval_id."""
    return _out(await svc().draft(ref_or_url))


class _Confirm(BaseModel):
    approve: bool = Field(description="Submit this application?")


def _elicit_approver(ctx: Context):
    """Human approval over MCP elicitation, if the client supports it (returns None when it does not)."""
    async def ask(summary: dict[str, Any]) -> bool | None:
        msg = (f"Submit application to {summary.get('company')} - {summary.get('title')}?\n"
               + "\n".join(f"{k}: {v}" for k, v in summary.get("answers", {}).items()))
        try:
            res = await ctx.elicit(message=msg[:3000], schema=_Confirm)
        except Exception:  # noqa: BLE001 - client without elicitation support
            return None
        if res.action == "accept":
            return bool(res.data.approve)
        return False
    return ask


class _Code(BaseModel):
    code: str = Field(description="The verification code you received by email or SMS")


async def _elicit_code(ctx: Context, row: dict[str, Any]) -> dict[str, Any]:
    """Awaiting a code (ADR-0026). 1) If the user granted `otp_assist` (ADR-0027), a code found in the latest mail
    from the expected sender is entered ONLY when the human clicks confirm on the masked prefill. 2) Otherwise
    (no grant, no mail, declined, or no elicitation) the USER types the code: via elicitation or the
    `provide_verification_code` tool. The code is never logged."""
    cfg = load_settings()
    masked = ""
    domain = cfg.verification.sender_domains.get(str(row.get("ref", "")).split(":")[0])
    if cfg.verification.assist and domain:
        res = await assist_code(cfg, sender_domain=domain, elicit=make_elicitor(ctx))
        if res.status == "confirmed" and res.code:
            code, res.code = res.code, None
            return await svc().provide_verification_code(row["pending_id"], code)
        masked = res.masked
    try:
        if not cfg.verification.elicit:
            return row
        hint = f" (the latest mail ends {masked}; it was not used)" if masked else ""
        res2 = await ctx.elicit(message="The site emailed/texted you a verification code to finish this application"
                                        f"{hint}. Enter it here (it is typed into the page and never stored).",
                                schema=_Code)
    except Exception:  # noqa: BLE001 - client without elicitation support
        return row
    if res2.action != "accept":
        return row
    return await svc().provide_verification_code(row["pending_id"], res2.data.code)


@mcp.tool()
async def submit_application(ctx: Context, ref_or_url: str, answers: dict[str, Any] | None = None,
                             dry_run: bool | None = None, approval_id: str | None = None,
                             token: str | None = None) -> str:
    """Submit using the best available path (platform API, else browser driver). Honors global dry_run; a per-call
    dry_run can only make it safer. `approval_id` is the server-issued id from plan_application. With
    approval_mode=human a person must approve (dialog, or `job-apply-mcp approve <id>`)."""
    if token is not None:
        return _out({"status": "error", "detail": "token is no longer accepted; use approval_id from plan_application"})
    row = await svc().submit(ref_or_url, answers=answers or {}, dry_run=dry_run, approval_id=approval_id,
                             approver=_elicit_approver(ctx))
    if row.get("status") == "awaiting_code" and row.get("pending_id"):
        row = await _elicit_code(ctx, row)
    return _out(row)


@mcp.tool()
async def provide_verification_code(pending_id: str, code: str) -> str:
    """The user types the code they received (email/SMS) for a submission in status `awaiting_code`. Ask the USER;
    never try to read their mail or guess. Only the format is checked; the code is typed into the open page, the
    page's verify button is clicked once, then the normal confirmation check runs. One attempt per code; the
    application is never resubmitted; after `verification.code_ttl_s` it becomes a `handoff`."""
    return _out(await svc().provide_verification_code(pending_id, code))


def _agent_mode() -> bool:
    try:
        return load_settings().apply.approval_mode == "agent"
    except Exception:  # noqa: BLE001
        return False


if _agent_mode():   # in approval_mode="human" resolving is CLI-only: the tool is not even exposed
    @mcp.tool()
    async def resolve_submission(ref: str, outcome: str = "", note: str = "", evidence: str = "") -> str:
        """Inspect (no outcome) or resolve a submission stuck in submitting/unknown. outcome: submitted|failed|
        handoff. Requires `evidence` = the evidence folder path reported for the ref. Audit-logged."""
        return _out(await svc().resolve_submission(ref, outcome, note, "agent", evidence))


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
async def improvement_report(limit: int = 10, cursor: int = 0) -> str:
    """Ranked improvement proposals learned from recent run failures (compact). Page with `cursor` (= `next`)."""
    rep = await svc().improvement_report(cursor + limit)
    props = rep.get("proposals", [])
    rep["proposals"] = props[cursor:cursor + limit]
    rep["next"] = cursor + limit if cursor + limit < rep.get("total", 0) else None
    return _out(rep)


@mcp.tool()
async def record_text(kind: str, text: str, origin: str, parent: str | None = None, model: str | None = None,
                      prompt: str | None = None, context: str | None = None) -> str:
    """Record provenance for a text artifact (kind: resume_bullet|cover_letter|answer|profile_fact).
    origin: user_written | llm_generated (llm_generated needs `model`). `context` = job/question the text answers."""
    return _out(await svc().record_text(kind, text, origin, parent=parent, model=model, prompt=prompt,
                                        context=context))


@mcp.tool()
async def verify_text(id: str, edited_text: str | None = None, verified_by: str = "user") -> str:
    """User approves LLM text. Without edited_text -> user_verified; with it -> user_edited child plus diff."""
    return _out(await svc().verify_text(id, edited_text=edited_text, verified_by=verified_by))


@mcp.tool()
async def provenance(id: str, include_text: bool = False) -> str:
    """Origin, model, prompt hash and ancestor chain for a text id."""
    return _out(await svc().provenance(id, include_text=include_text))


@mcp.tool()
async def health() -> str:
    """Report enabled platforms, LLM providers and which local models are reachable."""
    return _out(await svc().health())


from .control.tools import register as _register_control

_register_control(mcp, lambda: svc().settings)
from .tuning.tools import register as _register_tune

_register_tune(mcp, lambda: svc().settings)

from .profile.tools import register as _register_facts

_register_facts(mcp, lambda: svc().settings)

from .grants.tools import register as _register_grants

_register_grants(mcp, lambda: svc().settings)
from .accounts.tools import register as _register_vault

_register_vault(mcp, lambda: svc().settings)
from .ingest.tools import register as _register_ingest

_register_ingest(mcp, svc)


def main() -> None:
    """Stdio server (kept for 0.1 client configs). The full CLI lives in cli.py."""
    mcp.run()


if __name__ == "__main__":
    main()

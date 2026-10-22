# ADR-0021: Approval and submission safety (amends ADR-0011)

Status: accepted · Date: 2026-10-22 · Amends: ADR-0011 decision 3 (approval token), ADR-0009/0014 dry_run semantics

## Context
External review found three holes. (1) The ADR-0011 token was `sha256(ref + answers)`, which any client can compute,
so "approval" proved nothing. (2) A per-call `dry_run=false` could override a global `dry_run=true`, and `auto_approve`
could be set from env or a project file in a cloned repo. (3) A crash or timeout between the click and the stored
result could lead to a second, duplicate application on retry.

## Decision
1. **Server-held approvals** (`apply/approvals.py`). `plan_application` returns an opaque `approval_id`
   (`secrets.token_urlsafe(24)`) stored in `<data_dir>/approvals.db` with ref, destination URL, hash of the final
   answers, hash of file contents, created/expiry (`apply.approval_ttl_s`). `submit_application(approval_id=...)`
   requires it, and the answers must hash-match. Client-written text for unresolved keys rebinds the record (state
   back to `pending`, human approval cleared); an approved answer can never be overridden; without a cached plan the
   supplied answers must hash-match exactly. The self-computed sha256 path and the `token` argument are removed.
2. **`apply.approval_mode`**: `agent` (the caller may submit with the server-issued id) or `human` (default). In `human`
   mode the record must be marked approved by a separate channel: MCP elicitation (`ctx.elicit` confirm dialog showing
   company, title and an answers summary), or when the client lacks elicitation, the CLI `job-apply-mcp approve <id>`
   / `deny <id>` / `pending`. Until then submit returns `needs_human` with the command. Every issue / rebind / approve /
   deny / submit attempt / state change is written to `approval_audit` with the actor (`agent`, `human:elicit`,
   `human:cli`, `auto_approve`).
3. **Overrides can only tighten.** A per-call `dry_run=false` while global `dry_run=true` is refused (error, also in
   `apply_batch`). `apply.auto_approve` is read only from the user config file (project file and env values are stripped
   with a warning), logs a loud warning on startup, and is ignored when `approval_mode="human"`.
4. **Submission state machine** (table `submissions`, same db): `planned -> approved -> submitting -> submitted | failed |
   unknown | handoff`. `submitting` is committed BEFORE the browser/API call. Exceptions, cancellation and `error` results
   become `unknown`; `needs_human` -> `handoff`; `roadblock` -> `failed`. A ref in `submitting`, `unknown` or `submitted`
   is never auto-resubmitted (also across restarts); the tool returns state, detail and the evidence directory. Only
   `resolve_submission(ref, outcome)` (tool, or `job-apply-mcp resolve <ref> <outcome>`) moves `submitting`/`unknown` to
   `submitted|failed|handoff`; `submitted` also records the application for dedupe. Each ref has a stable idempotency
   key (`sha256(ref, url)`), stored and audited.

## Consequences
Default installs now require a human for every real submit (set `approval_mode = "agent"` for the old flow). The new
tables live in their own sqlite file so the `Store` protocol and `applications.db` schema are untouched. Answer text is
stored only as hashes plus a truncated summary for the approver. `apply_batch` returns `approval_id` in
`awaiting_approval` rows. A crash after the click requires one manual `resolve_submission`; that is deliberate.

## Verify / revert
`uv run pytest tests/test_approvals.py tests/test_apply_engine.py` (includes a test that a client-computed sha256 token
is rejected). Revert: revert the PR; ADR-0011 behavior returns (and with it the forgeable token, so do not).

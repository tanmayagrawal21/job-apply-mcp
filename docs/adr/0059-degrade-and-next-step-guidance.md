# ADR-0059: Missing components degrade to `needs_human`; every status carries `next_action` and `hint`

Status: accepted · Date: 2026-10-27 · Amends ADR-0005, ADR-0011

## Context
A client other than Claude Code drove the tool on an Ashby new-grad posting. The writing reviewer had no model
behind it and raised `ComponentMissing`. The tool guard turned that into an `ok:false` error, so the client
treated it as a crash and stopped. It had no reason it could act on and no next step. The same client also had
to guess what to do after statuses such as `handoff` or `unknown`.

## Decision
- **One guard per tool, unchanged in shape.** `core.output.guarded` already wraps every registered tool. It now
  sends `ComponentMissing` and `ProviderUnavailable` (an optional local model, reviewer, browser extra or
  profile) to `degraded()`, which returns
  `{"ok":false,"status":"needs_human","why":...,"next_action":"fix_setup","hint":...,"error":{...}}`. The
  FastMCP pre-call path (`install`) and `error_from_exc` return the same shape. Other exceptions keep the
  `error` envelope. In `apply_batch`, a ref that raises `ComponentMissing` becomes a `needs_human` row with
  its reason, and the rest of the batch goes on.
- **Next-step guidance.** `core/next_steps.py` maps each status to a snake_case `next_action` and a hint of at
  most 110 chars. The statuses are planned, needs_input, needs_human, needs_review, incomplete, handoff,
  unknown, roadblock, awaiting_code, awaiting_approval, submitted, dry_run, duplicate, skipped and error.
  Submit rows, `provide_verification_code`, batch rows and plan/draft results (which now also carry `status`)
  include the guidance. Every page of a paged plan repeats it, because it is a header scalar.
- Hints never include posting or profile data. They never suggest bypassing the tool or retrying a submit
  whose outcome is unclear.

## Consequences
A missing optional component no longer ends a session. The client reads `why` and `next_action` and tells the
user. Each result grows by about 120 chars.

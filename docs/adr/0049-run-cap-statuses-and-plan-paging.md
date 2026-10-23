# ADR-0049: Run cap in dry and live mode, one status per posting, plan paging

Status: accepted · Date: 2026-10-22 · Amends ADR-0005, ADR-0045 · Issue #71

## Context
QA (#71) found that `run_applications(max_applications=N, dry_run=True)` never showed the cap working: the cap only
counted postings that reached submit, so three postings handed to the owner all came back with `max_applications=1`,
and 0 and -1 were silently replaced by a default. A dry run said nothing about a live quota that would block the
live run (for example Greenhouse at 25/25). Statuses were unclear: `handed_off` in run reports and `needs_human` from
`submit_application` meant the same thing, and a dry-run success was `dry_run`, easy to read as "not done".
`plan_application` output over the response budget was trimmed by the generic shaper with `"omitted": N` and a
`next` cursor that could go negative and that no tool parameter accepted.

## Decision
1. **`max_applications` is validated.** A whole number >= 1, or omitted (default `intake.max_applications`, or
   run_size feedback). Anything else (0, negative, bool, float, string) is an error naming the value. A value above
   `intake.max_applications_cap` is clamped to it with a note in `plan_context.notes`; the cap is never raised.
2. **The cap counts eligible postings, the same in dry and live mode.** Eligible = ready to submit, or needing the
   owner after planning (review gate, unresolved required fields). In priority order, the first N eligible
   postings are handled; later ones are `skipped` with `max_applications=N reached` (mail messages stay
   unprocessed for the next run). Skipped (policy, duplicate, prose not yet approved), failed and unsupported
   postings never take a slot. In live mode this is stricter than before (a hand-off now uses a slot), never looser.
3. **One status per posting** (`intake.store.CATEGORIES`, documented in README and the tool docstrings):
   `submitted`, `confirmed`, `dry_run_ok`, `needs_human` (with `why`), `quota_blocked`, `skipped` (with `why`),
   `unsupported_site`, `unknown`, `failed`. Engine results map as: `dry_run` -> `dry_run_ok`; `needs_human`,
   `handoff`, `incomplete`, `awaiting_code`, `needs_review` and an awaiting-approval live submit -> `needs_human`;
   a `needs_human` whose detail is the control layer's `quota reached` -> `quota_blocked`; `duplicate` ->
   `skipped`; `roadblock` -> `unknown`; `error` -> `failed`. Rows stored before this ADR (`handed_off`, `dry_run`)
   are mapped on read, and `run_report(category=...)` accepts the old names.
4. **Dry runs preview the live quota.** `live_preview` = `{would_submit, quota_blocked, blocked: [{ref, by}],
   warning: [...]}` built from the control layer's read-only `daily_limit_status()` (the same data as
   `control_status`), walking the `dry_run_ok` rows in order against the day, run and per-platform remainders. It
   also warns when global `dry_run` or human approval mode would stop or hand off a live run. It reads limits only
   from the loaded (trusted) config and changes nothing; the engine's control layer still enforces every quota.
5. **plan_application / draft_answers paging.** New `cursor` parameter (default 0). A result that fits the budget
   is unchanged. A larger one is split by `core.output.page_sections`: scalars (ref, gate, approval_id, t) on every
   page; every dict key or list item of a section (answers, src, audit, unresolved, client_tasks, ...) is one item,
   packed in order until the page would pass the budget; the page carries `next` (null on the last page) and
   `total`. Dict sections stay dicts, so pages merge into the full result. `cursor>0` reads the last result for
   that ref from memory (32 most recent): no re-plan, no new approval_id; without one it is an error.
6. **The generic shaper never emits a bad cursor.** `next` belongs to one list (`paged`); a second list cut in the
   same object is recorded in `trimmed` {key: n} without a cursor, and the last-resort deep pass records `trimmed`
   only. `next` is never negative.

## Consequences
- Clients reading `handed_off` / `dry_run` keys in fresh run reports must read `needs_human` / `dry_run_ok`.
- A live run can now hand off fewer than before when hand-offs come first; that is the intended cap semantics.
- No quota, gate, approval or dry-run control is loosened; quota settings are still honoured only from the
  trusted user config (ADR-0035).

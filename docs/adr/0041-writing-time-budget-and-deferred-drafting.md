# ADR-0041: Prose time budget, heavy-model semaphore timings, deferred drafting

## Context
QA issue #52: with `writing.reviewer = "router"` on a 16 GB machine shared with other Ollama users, one
`plan_application` took over 200 s (one call about 6 minutes). Causes:
- No budget spanned the whole prose pipeline. Each call had its own `asyncio.wait_for`, but a field can run up to
  `max_rounds` writer, reviewer and revise calls, each of which can fall back to another candidate.
- The per-call `wait_for` ran *inside* the provider and heavy semaphores, so time spent queued was not bounded or
  reported.
- The Ollama HTTP client used one provider-wide timeout (`llm.timeout_s`), not the tier's own.
- Only tiers flagged `heavy` were serialized, so a local review model could compete with the narrative model for
  memory.

## Decision
1. **`writing.timeout_s` (default 90 s; `<= 0` turns it off).** This is one wall-clock deadline for every prose field of
   a plan. It covers writer, reviewer and revise rounds, and fields still queued on `concurrency.global_limit`. Each
   field runs under `asyncio.timeout_at(deadline)`. On expiry the in-flight router call is cancelled, which closes the
   HTTP request so Ollama stops generating. `write_text` updates a `WriteProgress` after every step. An unfinished
   field becomes a `client_task` with `timed_out: true`, `stage` (writer/review/revise/queued), and, when one exists,
   the partial `draft` plus the review rubric. Planning then finishes as usual. A timed-out draft appears only inside
   the task. It is never added to `answers`, so it is never part of the cached plan, the approval record, or a
   submit. The key stays in `unresolved`, so a dry-run submit returns `needs_human`.
2. **Per-call timeout in the provider.** The router passes the tier budget as `timeout_s` to providers that declare
   `supports_timeout` (ollama, openai_compat, fake). The HTTP timeout now matches the tier, and `wait_for` stays as an
   outer bound. That bound covers generation only. Queue wait is bounded by the caller's deadline (item 1).
3. **Global heavy semaphore by task class.** `llm.heavy_classes` (default `["narrative", "review"]`): every local
   (non-cloud, non-client) call of those classes takes the router-wide `heavy_parallel` semaphore, whatever the
   tier's `heavy` flag. The router measures queue wait (provider semaphore plus heavy semaphore) separately from
   generation. It adds both to a per-request sink (`llm.base.LLM_TIMINGS`, a contextvar that the engine sets per plan),
   which then appears in `timings` as `llm_wait` and `llm_gen` (ms). `health().stats` gains `avg_wait_ms`.
4. **`writing.mode = "inline" | "deferred"` (default inline, which keeps the old behaviour).** In deferred mode,
   `plan_application` does deterministic mapping and the one batched small-model call only. Prose fields are listed
   immediately as `client_tasks` with `deferred: true`. The new MCP tool `draft_answers(ref)` later runs writer plus
   reviewer for the plan's open fields under the same budget. It merges passing drafts into the cached answers and
   re-issues the approval over the larger set.

## Consequences
- With the default 90 s budget, the worst-case cost of prose per plan is bounded. Batch planning in deferred mode
  costs only fetch plus the small model.
- The budget is per plan, and `apply.plan_workers` plans may run at once. The heavy semaphore is per process, so
  separate server processes and other Ollama clients still contend inside Ollama. That contention shows up as
  `llm_gen`, and the per-call timeout bounds it.
- On slow hardware the writer can time out before producing anything (`stage: writer`, no draft). The calling agent
  then writes the answer itself, or the user raises `writing.timeout_s`.

## Measured (one dry-run `plan_application` over MCP stdio; Greenhouse posting with a required "Why ..." question;
temp browser profile; writer `gemma4:12b-it-qat` with the router reviewer; 16 GB Apple Silicon; another model resident)
| run | wall | `t` |
|---|---|---|
| before (main) | 486.9 s | write 481557, llm 481676 |
| after, inline | 101.0 s | write 90003, llm_wait 0, llm_gen 91113 (field `timed_out`, stage writer) |
| after, deferred plan | 13.1 s | llm 832, llm_gen 705 (small model only), fetch 12175 |
| after, draft_answers | 90.0 s | write 90005, llm_wait 0, llm_gen 90703 |

## Verify
`uv run pytest -q tests/test_writing_budget.py`: the budget is respected and the call is cancelled; a partial draft
becomes a client_task and never an answer; the heavy semaphore shows `llm_wait` > 0 and peak concurrency 1; deferred
mode makes no narrative or review calls in plan; `draft_answers` fills the field.

## Revert
Set `writing.timeout_s = 0` (no budget), `llm.heavy_classes = []` (only `heavy` tiers serialize), and keep
`writing.mode = "inline"`. To revert the code, revert this PR. No data format changed.

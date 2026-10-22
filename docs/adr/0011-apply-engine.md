# ADR-0011: Apply engine, approval tokens, client_tasks, pipelined batch

Status: accepted · Date: 2026-10-22

## Context
Applying has to be fast (many jobs, slow browsers, slow local LLMs), safe (no unreviewed submissions, no
double applications) and token-frugal. The engine must not depend on how profile, router, gates or store are
implemented, because those components are built in parallel.

## Decision
1. **Collaborators are injected** and typed by Protocols (`apply/ports.py`, `core/interfaces.py`);
   `apply/adapters.py` wraps the concrete profile, `policy.gates.evaluate_posting` and `llm.router.LLMRouter`.
   `apply/factory.py` imports siblings lazily and raises `ComponentMissing` with the branch name when absent.
   An optional `ControlLike.check("submit", ctx)` (ADR-0014) can block or hand off before any submit.
2. **plan(ref)** = (posting + form fetched concurrently) -> (gates + dedupe concurrently) -> deterministic mapping
   (`mapper.py`) -> ALL remaining short fields in ONE batched router call (JSON keyed by field key, each value
   validated against options/length/type and a confidence floor) in parallel with the prose pipeline
   (ADR-0013) for required long text -> anything left becomes a `client_task`
   `{key,label,max_len,context:{role,posting,facts},options?,draft?}`.
   Fields about legal/biographical facts (sponsorship, authorization, demographics, "years of ...", attestations,
   clearance, citizenship) are never sent to an LLM; without a profile answer they become client_tasks.
   Optional long text is skipped unless `apply.write_optional`.
3. **Approval token** (borrowed, ADR-0006): `sha256(canonical_json({ref, answers}))` over the server-generated
   answers. `submit` accepts (a) the token of the cached plan, in which case client-written answers may only
   fill the unresolved keys and may never alter an approved answer, or (b) the token of the exact full answer set
   (stateless, survives a restart). Real submits without a valid token are refused unless `apply.auto_approve`.
   Dry runs need no token.
4. **submit** re-checks dedupe inside the per-company lock (`store.company_lock`, own lock as fallback), then
   control layer, then path: `adapter.submit` (API) -> browser driver -> `needs_human`. Result is recorded for
   submitted / needs_human / roadblock (the store only treats `submitted`/`duplicate` as "seen"). Nothing is
   recorded for dry_run/error.
5. **Pipelined batch**: separate pools, planning (`apply.plan_workers`) and browser (`concurrency.browser_workers`
   semaphore in the engine, optional `max_parallel` cap), all refs start at once, so planning and prose for job N+1
   overlap the browser work for job N. Without `auto_approve` and with `dry_run=false`, a batch stops at
   `awaiting_approval` with tokens; fully unresolved plans return `needs_input` with client_tasks.
6. **Timings**: each stage (`fetch,gate,map,llm,write,submit`) records ms in the result under `t`.

## Consequences
Tool output stays small (answers keyed by field, tasks only for what is truly unresolved). The in-memory plan cache
(TTL `apply.plan_ttl_s`) saves a refetch on submit; a restarted server falls back to the full-answer token.
`submit_application` gained an optional `token` argument.

## Verify / revert
`uv run pytest tests/test_apply_engine.py tests/test_apply_mapper.py tests/test_apply_wiring.py`.
Revert: drop `apply/` engine files and the three `Services` methods return to `NotImplementedError`.

# ADR-0011: Apply engine, approval tokens, client_tasks, pipelined batch

Status: accepted · Date: 2026-10-22 · Decision 3 (approval token) amended by ADR-0021: approvals are now server-held ids

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

## Amendment 1: dogfood invariants (mapper, engine, gates)
A real planning run produced wrong answers. These invariants now hold and are covered by `tests/test_apply_dogfood.py`:
1. **Option matching is negation-aware and thresholded.** Options are tokenized; negators (not/no/never/unable/
   cannot/n't/without) set polarity, and a chosen option must share the answer's polarity (Yes never lands on
   "I am not able to relocate"). Fuzzy matches need content-word overlap, `apply.option_min_score`, and no tie.
   Below threshold -> unresolved, never a guess.
2. **Demographic/pronoun/EEO fields** come only from `profile.demographics`, matched exactly/near-exactly (token sets),
   else the decline option, else unresolved.
3. **"How did you hear"** uses `apply.referral_preference` (default Job board, Other job board, Other, LinkedIn);
   Referral/Employee options are chosen only if the profile value itself says referred.
4. **No fabricated values.** URLs, emails, phones, usernames and links come only from the profile; such questions are
   never sent to the LLM, and `looks_fabricated()` rejects placeholder-ish LLM output (example.com, user@..., 555-01xx).
5. **Derived geo fields** (`latitude`/`longitude`) are filled from optional `contact.lat`/`contact.lon`, else omitted;
   never client tasks and never block submit.
6. **Dates/terms** (graduation) are derived deterministically from profile education `end`; the LLM never sees them.
7. **Attestations, certifications and AI-use questions** are never LLM-answered; without an explicit profile
   `[answers]` pattern they become `needs_human` client tasks (batch status `needs_human`).
8. **LLM short answers are grounded**: experience/skill yes-no claims need the topic in profile facts; numbers, links
   not in the facts are rejected -> unresolved.
9. **Title gate** (`title`): skips "PhD Graduate", "New PhD", "PhD student/candidate" titles unless `policy.has_phd`,
   plus user `policy.exclude_title_patterns` (regexes). The `phd_only` text flag was widened the same way.

Verify: `uv run pytest tests/test_apply_dogfood.py`. Revert: revert the amendment commit; behavior returns to ADR-0011 base.

## Amendment 2: dogfood round 2 (option intent, synonyms, deterministic salary)
1. **Option intent layer** (`mapper.option_intent`). Yes/No profile answers over multi-option selects classify each
   option as affirmative (willingness words: willing/open/able/relocating/yes...), negative (negators) or neutral
   ("I am local to the area"). Yes -> the most specific affirmative (most willingness words); a tie, or only
   neutral/negative options -> unresolved. No -> the single negative option (lead "No" preferred), else unresolved.
2. **Answer-pattern synonyms** (`profile.DEFAULT_ANSWER_SYNONYMS`, extendable with `answer_synonyms` in the profile).
   A `[answers]` pattern that belongs to a synonym group matches a question containing any member ("in office" ~
   "in person" ~ "on-site" ~ "office culture").
3. **Deterministic salary** (`apply/salary.py`, `PolicyCfg.base_salary|default_ask|salary_round_to|
   salary_multi_level_ratio|salary_lower_middle|col_multipliers`). floor = base (or `salary_floor`) x COL multiplier of
   the posting location. With a posted range: ask = min(max(anchor, floor), top) where anchor is the midpoint, or the
   lower-middle when top/bottom exceeds the ratio; no range: max(floor, default_ask). Rounded to a clean number, never
   above the posted top. Number fields get `115000`, text `$115,000`, selects the option whose range contains the ask.
   "Current salary"/history fields are never auto-answered. The computation is recorded in `ApplicationPlan.audit`
   (and `compact()["audit"]`); answers carry source `rule:salary`. Nothing computable -> previous behavior.
Verify: `uv run pytest tests/test_apply_dogfood2.py`. Revert: revert the amendment commit.

# ADR-0008: Routing and complexity negotiation

Status: accepted · Date: 2026-10-22

## Context
Form answers range from "Yes" to a 300-word cover letter. A 16 GB Mac can hold one large local model at a
time. Cloud and the calling agent are stronger but cost tokens and privacy. We want the cheapest adequate
model per task, automatic fallback, and no hidden hard-coding. Cloud use is a user policy (`allow_cloud`).

## Decision
`llm/router.py` (`LLMRouter`) routes in this order.

1. **Task class first** (`Task.task_class`, else auto-detected):
   - `narrative`: cover letters, why-us answers, any prose of at least `routing.narrative_min_words` (60)
     words (estimated from `expected_chars`/6), or a question matching `routing.narrative_keywords`.
     Uses `llm.task_tiers.narrative` (default ollama `gemma4:12b-it-qat`, 1200 tokens, temperature 0.6, `heavy`).
   - `review`: a second reader over a draft (`router.review()`). Uses `llm.task_tiers.review`
     (default provider `client`, i.e. the calling agent; point it at any local or cloud model).
   - `field`: everything else, scored for complexity.
2. **Complexity for `field`** (cheap rules, all points in `llm.routing`): field kind, expected length,
   essay keywords, company-specific reasoning, prior validation failures, and an optional small-model
   confidence self-check (`self_check`, off by default). Thresholds (`medium` 2, `large` 5) map the score
   to the tier in `llm.tiers` (small/medium/large).
3. **Provider choice**: the tier's primary plus `llm.fallbacks[tier]`; the first one that is available
   (provider probe cached `availability_ttl_s`; Ollama also checks the model is pulled) wins.
   `allow_cloud=false` skips providers with `is_cloud` (anthropic, remote OpenAI-compatible hosts,
   litellm, client), so tiers degrade to local fallbacks.
4. **Calling**: per-provider semaphore from `concurrency.llm_parallel`; models flagged `heavy` also share one
   global semaphore (`llm.heavy_parallel`, default 1) so a single big model is resident. `ollama.keep_alive`
   (default `5m`) is passed on every request. Timeout `llm.timeout_s`; transient errors retry `llm.retries` times
   with exponential backoff `llm.backoff_s`.
5. **Validation**: with `json_schema` (or a `validator`), a bad reply gets one repair attempt on the same model,
   then escalates one tier (field tasks) or to the last tier (narrative/review). The top tier failing returns
   `ok=false` with the error; the caller decides (typically `needs_human`).
6. **Client tier**: the `client` provider makes no generation call. If the MCP client advertises sampling
   (`current_sampler`, set by server wiring via `sampler_from_context(ctx)`), it samples; otherwise the router
   returns a `ClientTask` (`client_task` in the result) for the calling agent to write.
7. **Observability**: each result carries `task_class`, `tier`, `score`, `reasons`; `health()` reports
   reachable providers and models, configured tiers and task tiers, per-class counts, escalations, and
   per provider:model calls, errors, tokens and average latency.

LiteLLM is an optional extra (`litellm>=1.83.0`, version re-checked at runtime; see ADR-0006). Nothing else
imports it.

## Speed (all knobs in `llm.*`, defaults conservative)
1. **Deterministic first.** `Task.known` or any `router.resolvers` callable that returns a value short-circuits
   with no model call (`task_class="rules"`, provider `rules`).
2. **Batch.** `run_batch(tasks)` resolves rules first, then sends ONE JSON-object call for all short fields
   (`routing.batch_enabled`, `batch_max_fields` 12, `batch_max_expected_chars` 200). Keys missing from the
   reply fall back to individual calls; long, company-specific or retried fields are never batched.
3. **Warm.** `providers.ollama.keep_alive` defaults to `30m`. `router.warmup()` preloads `llm.warmup_tiers`
   (default `small`) via an empty `/api/generate`; the first routed call also starts it in the background
   (`llm.warmup`). Servers can call `router.start_warmup()` at startup.
4. **No thrash on 16 GB.** `small` and `medium` are the same resident model (qwen3:4b); `medium` only
   turns reasoning on (`think=true`, longer budget). `think=false` is sent for short fields (Ollama `think`,
   `/no_think` for OpenAI-compatible qwen3). The narrative model loads only for prose, and `heavy` models
   share one semaphore (`heavy_parallel=1`).
5. **Small contexts.** `num_ctx` per tier (4096 short, 8192 narrative) and tight `max_tokens` (`num_predict`).
6. **KV-cache reuse.** Each class has a fixed `system_prefix`; variable content is only in the user turn.
   `Task.system` overrides it.
7. **Latency budgets.** `LLMTier.timeout_s` (small 25 s, medium 60 s, narrative 150 s). A timeout is not
   retried (`retry_on_timeout=false`); the router tries the next candidate, then escalates, and if everything
   fails returns a `client_task` for the calling agent (`client_on_failure`, needs `allow_cloud`).
8. **Parallelism.** `concurrency.llm_parallel` accepts `provider` or `provider:model` keys; ollama defaults
   to 2. Set it equal to `OLLAMA_NUM_PARALLEL`.
9. **Latency stats.** `health()` reports calls, errors, tokens, avg, p50 and p95 (last 200) per provider:model,
   plus warm state.
10. **Benchmark.** `scripts/bench_llm.py` (live, optional) prints cold start, warm latency and tokens/s per
    configured local model.

## Consequences
Cheap fields never wake the 12B model; narratives get a better model with a hot-swap guard. Rule scoring is
crude but transparent and tunable. Without Ollama, `client` fallbacks hand work back to the caller.

## Verify / revert
Verify: `uv run pytest -q tests/test_llm.py`; `health` shows `llm_router`. Optional live check:
`JAM_LIVE=1 uv run pytest -k live`. Revert: set `llm.task_tiers = {}` to disable classes, or delete
`src/job_apply_mcp/llm/`; services only lazily imports the router.

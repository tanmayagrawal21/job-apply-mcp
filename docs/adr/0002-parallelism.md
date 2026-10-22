# ADR-0002: Parallelism is a first-class requirement

Status: accepted · Date: 2026-10-22

## Decision
- All I/O is `async` (httpx.AsyncClient, one shared client per process with HTTP/2 keep-alive).
- **Fan-out** over boards and postings uses `bounded_gather` with a global cap
  (`concurrency.global_limit`), plus a `HostLimiter` that caps concurrency per host and spaces requests
  (`per_host`, `min_interval_s`). Throughput scales with config, and no single host is hammered.
- **Applications** run in a worker pool (`apply_batch`, `browser_workers`). Each worker owns one browser
  context, so there is no shared tab state.
- **LLM calls** have per-provider semaphores (`llm_parallel`). On a 16 GB Apple-silicon machine Ollama
  defaults to 1 concurrent generation, while remote providers default to 4.
- **Errors are values:** a failed task never cancels its batch. Results keep input order.
- **Idempotency:** before submitting, every worker re-checks the duplicate index under a per-company
  lock. This prevents the "two agents applied to the same job" failure seen in manual runs.

## Verify / revert
`pytest tests/test_core.py -k "bounded_gather or host_limiter"` (ordering, cap enforcement, per-host spacing, error isolation).

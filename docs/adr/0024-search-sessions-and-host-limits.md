# ADR-0024: Search sessions and per-host rate limits

Status: accepted · Date: 2026-10-22

## Context
Searching ~1,900 boards took over 15 minutes for the first page. Two causes: (1) pagination was stateless, so every
`cursor` call re-ran the whole fan-out; (2) one global `per_host=4` / `min_interval_s=0.25` applied to every host, so
all Greenhouse boards (one API host) were throttled to about 4 requests/second at best, and the limiter held a single
lock while sleeping, serialising spacing across all hosts.

## Decisions
1. **Search sessions** (`search/sessions.py`). The first `search_jobs` call builds the filtered, ranked, compact rows
   once and stores them in an in-memory LRU (`search.sessions.max_sessions`) with a TTL (`ttl_s`, refreshed on use)
   and optional JSON files under `<data_dir>/search_sessions` (`persist`). `next` is the opaque `<session_id>:<offset>`;
   later pages read the session with no network. Unknown/expired/malformed cursors return `{error}` telling the
   caller to rerun without a cursor. Legacy integer cursors still work (they start a fresh search at that offset).
   `sessions.enabled=false` keeps integer `next` values (each page still reuses nothing; escape hatch only).
2. **Per-host limits.** `concurrency.hosts = {host: {per_host, min_interval_s}}` overrides the global pair. Defaults:
   boards-api.greenhouse.io 16 / 0.02 s, api.ashbyhq.com 8 / 0.05 s, api.lever.co 8 / 0.05 s; every other host keeps
   4 / 0.25 s. `global_limit` rose 16 -> 32 so the fan-out can actually fill a 16-wide host. `HostLimiter` now keys by
   hostname (port ignored), reserves spacing slots under a per-host lock and sleeps outside it.
3. **Progress.** `search_jobs` takes a `Context` and calls `ctx.report_progress(done, total)` (a no-op when the client
   sent no progress token). New tool `search_status(session_id)` reports status, boards done/failed/total, rows and
   elapsed time; the session is registered before the fan-out so a concurrent call can poll it.
4. **Benchmark.** `scripts/bench_search.py` (live, opt-in) prints boards/sec, time-to-first-page and page-2 latency;
   `--legacy` reproduces the old limits.
5. `server.py` change is limited to the `search_jobs` signature (`cursor: str`), the progress shim, and the new tool.

## Consequences
Memory: up to `max_sessions` x `max_rows` (5000) compact rows. Sessions are per server process unless `persist` is on.
Higher request rates to three public APIs are intentional but configurable; set `concurrency.hosts = {}` to restore
the old behaviour.

## Verify / revert
`uv run pytest tests/test_search_sessions.py`; live: `uv run python scripts/bench_search.py --boards-file b.txt`.
Revert: set `search.sessions.enabled=false` and `concurrency.hosts={}`, or revert the PR.

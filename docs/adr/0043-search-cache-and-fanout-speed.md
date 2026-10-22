# ADR-0043: Search speed: memoized board parsing, cheaper text flags, interleaved fan-out

## Context
Issue #60: `search_jobs` over 1,887 boards (840 Greenhouse, 710 Ashby, 337 Lever) took 177-247 s, and an immediate
repeat was barely faster. A profile of a real MCP stdio search on main (`since_days=21`, `limit=50`, fresh data dir)
showed:

- Cold: 333 s. Warm (same server, inside `cache_ttl_s`): 208 s.
- On the warm run all 1,807 live boards were fresh cache hits. Fresh hits already skipped the network and the
  `HostLimiter`. Ashby responses were cached under a stable key. Neither of the two suspected causes was the problem.
- The time went to CPU on the event loop. Each search re-read about 1.4 GB of cached JSON and rebuilt every posting:
  `html_to_text`, then `flag_text` (about 1.3 ms per description, from IGNORECASE alternations), then
  `extract_salary`. That covered about 97k postings, including the half that the `since` filter then dropped. While
  the loop was blocked, the few requests still in flight showed 60-85 s latencies.
- Each search also re-requested 80 dead boards (404s were not cached).
- The first cache load in each process parsed every cache file to prune it.
- Targets were ordered by platform, so all 32 global slots filled with Greenhouse boards first. The hosts then ran
  one after another instead of in parallel.
- Ranking 34k rows took 0.5-1.3 s. Ranking and dedupe are linear (plus the sort), so they are not the bottleneck.

## Decision
1. **Memoized board parsing.** `HttpClient.get_derived(url, derive, tag=...)` keeps `derive(body)` in memory, keyed
   by the response's content digest (sha1, also persisted in the cache entry). A fresh entry with a matching digest
   returns the memo without touching disk, network or limiter. A 304 keeps the digest, so the memo also survives
   revalidation. The memo is an LRU of `platforms.memo_boards` entries (default 4096). The Greenhouse, Lever and Ashby
   board listings use it.
2. **Date filter before parsing.** Adapters drop postings older than `since` using the raw date field, before
   building postings. The memo tag includes `since`, so different windows never share a list.
3. **Cheaper text flags with identical results.** Each flag pattern has a necessary lowercase substring (`_NEEDS`;
   for example, every no-sponsorship match contains "sponsor"). The pattern is skipped when its substring is absent
   from `_low(text)`. A `(?=[first chars])` guard rejects impossible start positions; IGNORECASE applies to the class
   exactly as to the pattern. This is 3.3x faster.
   - **The prefilter never decides a flag result, it only skips work.** `re.IGNORECASE` also matches four non-ASCII
     characters to ASCII letters: U+0131 `ı` and U+0130 `İ` to `i`, U+017F `ſ` to `s`, and U+212A (Kelvin) to `k`.
     `str.lower()` leaves the first three unusable (it does not map `ı`/`ſ`, and it expands `İ` to two characters).
     `_low` therefore maps all four before lowercasing. A first version used plain `text.lower()`, and the review found
     that "cıtızen" and "ſponsorship" lost their gate flags.
   - **Tests.** A property test compares `flag_text` with the plain `_PATTERNS` for every pattern, over samples and
     their confusable variants. It also compares `extract_salary` with the prefilter on and off. A second test scans
     all of Unicode and asserts that every IGNORECASE alias of a needle character folds to it under `_low`. With only
     `ı`/`ſ` mapped, both tests fail (on `İ`).
   - **Corpus check.** On 23,842 real descriptions there were 0 mismatches.
4. **Negative caching.** A 404 is stored as a negative entry for the TTL. A stale negative entry is refetched and is
   never served as stale-if-error.
5. **Prune by mtime.** The startup prune reads the file mtime (equal to the last store) and never parses the file.
6. **Interleaved fan-out.** `Services._targets` round-robins boards across platforms. The per-host semaphores then
   run in parallel. `concurrency.global_limit` goes from 32 to 48, which is at least the sum of the default per-host
   caps (40). The per-host politeness limits of ADR-0024 are unchanged; Lever stays at 8. A user who wants more can
   raise `concurrency.hosts."api.lever.co".per_host` in their own config.
7. **Per-board timeout.** `search.board_timeout_s` (default 60) bounds each board. A board over budget is reported in
   `errors` as `timed out after Ns`. Its fetch is shielded and keeps running in the background, so the response still
   reaches the cache for the next search. `Services.aclose` cancels leftover fetches.
8. **`timings` in the search response.** It contains:
   - `total_s`, `fetch_s`, `filter_s` and `rank_s`: wall times.
   - `network_s`, `cache_s` and `derive_s`: seconds summed over concurrent requests, so they can exceed wall time.
   - `n`: counts of requests, cache hits, memo hits and revalidations.
   - `slowest`: the three slowest boards.

## Results (real MCP stdio, 1,887 boards, `since_days=21`)
| run | before | after |
|---|---|---|
| cold (empty cache) | 333 s | 149-154 s |
| first warm repeat, same server (re-fetches Lever boards that timed out on the cold run) | 208 s | 14-28 s |
| later warm repeats, same server, inside TTL | 208 s | 1.5 s |
| warm, new server process (disk cache only) | n/a | 66 s |

An earlier head with Lever `per_host=16` measured 106 s cold. That change was reverted in review (see 6).

## Consequences
- The warm path is effectively free. A new process still parses the disk cache once: about 27 s of JSON decode and
  about 38 s of derive.
- The cold path is now bound by Lever bandwidth at 8 connections (three boards take 55-92 s each, and some hit the
  60 s board timeout) and by about 40-60 s of parse CPU.
- Possible follow-ups:
  - Parse in a process pool, off the event loop.
  - Page very large Lever boards in parallel.
  - Persist derived postings next to the cache, so a new process is also fast.
- Memory: the memo keeps finished postings without descriptions, about 46k for this board set.

## Verify / revert
- Run `uv run pytest tests/test_search_bench.py tests/test_http.py tests/test_textflags.py -s`. The benchmark prints
  cold and warm times with a fake transport. Warm must make no request, take no limiter slot and run no derive.
- Live check: the `timings` field of a real `search_jobs` call.
- Revert by reverting this PR. Cache entries gain `digest` and `status` keys that older code ignores. Old negative
  entries (empty `body`) would fail to decode in older code until they expire (the TTL), so after a revert, clear
  `<data_dir>/http_cache` if 404 boards show JSON errors.

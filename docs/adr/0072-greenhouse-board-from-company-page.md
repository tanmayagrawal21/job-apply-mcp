# ADR-0072: Greenhouse board token for a company-hosted ?gh_jid= page is read from the page and verified

Status: accepted · Date: 2026-10-28 · Relates to ADR-0069

## Context
A pasted company careers URL with `?gh_jid=` (no greenhouse.io host) got its board token from the
host's registrable label. Hosts like `careers.with<brand>.example` gave `with<brand>`, the jobs API
returned 404, and the application was skipped as "job not found" (issue #116). `greenhouse:<board>:<id>`
refs were fine.

## Decision
- `GreenhouseAdapter.get_posting` / `get_form` resolve such a URL asynchronously (`_resolve_job`).
  Candidates, in order: tokens the page names (embed script `boards.greenhouse.io/embed/job_board/js?for=`,
  an `embed/job_app?...for=` iframe, a `(job-)boards.greenhouse.io/<board>/jobs/<id>` link), the page's
  `og:site_name` as a slug, then host variants (the registrable label, then that label without a common
  careers prefix or suffix such as `with`, `join`, `careers`, `jobs`). At most 6 candidates.
- The page is fetched once with the shared `HttpClient.get_text` (same rate limiter, timeouts and retries,
  no browser). Page content is untrusted: only strings that fully match `[a-z0-9][a-z0-9-]{0,62}` are
  kept, and nothing is used until the public jobs API confirms that `<board>/jobs/<gh_jid>` exists.
- The first verified board is cached per host in-process, and a per-host lock means the concurrent
  `get_posting` + `get_form` calls fetch the page only once. When nothing verifies, or the page cannot be
  fetched, the old host-label guess is used, so the existing "job not found" path is unchanged.

## Amendment (review follow-up to #117): cache key
The board cache was keyed by host only, so a second `gh_jid` on the same host whose job lives on another board
(a multi-brand careers site) reused the cached board, got a 404 and was skipped as closed. Now:
- A verified board is cached per `(host, gh_jid)`; an exact hit costs nothing.
- For a new `gh_jid`, the host's last verified board is tried first, and is used only after
  `<board>/jobs/<gh_jid>` verifies. On a miss the full page lookup runs (the missed board is not re-checked as a
  candidate), and the board that verifies becomes the host's new last board.

## Consequences
Pasted company URLs resolve to the real board. A resolution costs one page GET plus up to 6 small API
GETs (cached by the HTTP layer). Refs and greenhouse.io URLs never fetch a page. The browser driver,
frame resolution and fill/submit logic are unchanged.

## Verify
1. `uv run pytest -q tests/test_greenhouse_board_from_page_116.py`, then the full `uv run pytest -q`. The
   cache-key amendment is covered by `test_second_gh_jid_on_same_host_on_another_board_is_not_skipped` and
   `test_second_gh_jid_on_same_board_reuses_host_board_without_page_fetch`.
2. Dry run, never live: `applywise run --dry-run --source links --link <company careers URL with ?gh_jid=>`. The
   plan should name the real board in the ref and must not end in "job not found".

## Revert
Revert the squash commit of this change (#117). No data migration is needed. The per-host board cache is
in-process only, and the HTTP cache entries it created are ordinary GET responses that expire on their own. After a
revert, pasted `?gh_jid=` URLs go back to the host-label guess. To revert only the cache-key amendment, revert its
follow-up commit; the cache goes back to per-host (a second job on another board of the same host is then skipped).

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

## Amendment (review follow-up to #117): response size cap
`HttpClient.get_text` used to download the whole body before the 2M-char slice in `page_board_candidates`. It now
streams the body and stops reading after `max_bytes` (default `MAX_TEXT_BYTES` = 2 MB of decoded bytes), then
closes the connection. The result is a truncated page, never an error. Timeouts, retries, the host rate limit,
redirects and decoding (charset, gzip) are unchanged. The cap is in the shared client, so the other `get_text`
caller (the Lever apply page) gets the same cap. JSON requests (`get_json`, `post_json`) are not capped.
Truncation is reported: `get_text_capped` returns `(text, truncated)`, where truncated is True only when the body
was longer than the cap (one byte past the cap is read to tell). The Lever apply page fails closed: if it was cut,
`get_form` raises an `AdapterError` ("tool_gap: ... text cap") instead of returning a form whose custom-question
cards past the cut would be silently missing, so no API submit is ever built from a partial page. Greenhouse only
loses board candidates on a cut page (each is verified against the API anyway), so it logs the cut and continues.

## Consequences
Pasted company URLs resolve to the real board. A resolution costs one page GET plus up to 6 small API
GETs (cached by the HTTP layer). Refs and greenhouse.io URLs never fetch a page. The browser driver,
frame resolution and fill/submit logic are unchanged.

## Verify
1. `uv run pytest -q tests/test_greenhouse_board_from_page_116.py`, then the full `uv run pytest -q`. The
   cache-key amendment is covered by `test_second_gh_jid_on_same_host_on_another_board_is_not_skipped` and
   `test_second_gh_jid_on_same_board_reuses_host_board_without_page_fetch`. The size cap is covered by
   `uv run pytest -q tests/test_http_text_cap.py` (an endless body stops at the cap; truncation is flagged only past the cap), and
   `tests/test_lever_cards.py::test_truncated_apply_page_fails_get_form_closed`.
2. Dry run, never live: `applywise run --dry-run --source links --link <company careers URL with ?gh_jid=>`. The
   plan should name the real board in the ref and must not end in "job not found".

## Revert
Revert the squash commit of this change (#117). No data migration is needed. The per-host board cache is
in-process only, and the HTTP cache entries it created are ordinary GET responses that expire on their own. After a
revert, pasted `?gh_jid=` URLs go back to the host-label guess. To revert only the cache-key amendment, revert its
follow-up commit; the cache goes back to per-host (a second job on another board of the same host is then skipped).
To revert only the size cap, revert its commit; `get_text` then reads whole bodies again.

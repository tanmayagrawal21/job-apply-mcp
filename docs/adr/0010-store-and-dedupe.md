# ADR-0010: SQLite store and duplicate detection

Status: accepted · Date: 2026-10-22

## Context
The same job must never be applied to twice, including by parallel workers (ADR-0002) and across
re-listed postings with new ids or lightly reworded titles.

## Decision
- `store/sqlite.py`: stdlib `sqlite3`, WAL mode, in `paths.data_dir/applications.db`; blocking calls run
  via `asyncio.to_thread` behind one connection and a thread lock. Tables `applications`
  (ref PK, company, title, company_key, title_key, status, via, confirmation, created_at) and `events`
  (append-only history). `company`/`title` raw columns are kept for display and CSV.
- Keys: company key lowercases, drops punctuation and legal suffixes (Inc, LLC, ...). Titles are token
  sets minus parentheticals, requisition ids and filler words; two titles match at Jaccard >= 0.75 and
  identical level tokens (I/II, senior, staff, intern, ...), so "Engineer I" != "Engineer II".
- `seen()` counts only `submitted`/`duplicate` rows within `dedupe_days` (config; per-call override). Dry
  runs and needs_human never block a later real attempt. `created_at` resets when status changes.
- `company_lock(company)` returns a per-company `asyncio.Lock`; the apply engine holds it across
  seen -> submit -> record.
- CSV import (insert-or-ignore by ref, idempotent) and export; `list()` filters by status/company/since
  and paginates newest first with an offset cursor. `Services.list_applications` returns compact rows.

## Consequences
Single-process locks only; two server processes sharing one DB could race (documented limit). Fuzzy
matching may rarely merge distinct roles at one company; raise the threshold or lower `dedupe_days`.

## Verify / revert
`uv run pytest tests/test_store.py -q`. Revert: delete `store/sqlite.py` and the `Services.store` wiring.

## Amendment 1 (2026-10-22): identity-first dedupe
Company + fuzzy title merged distinct requisitions that share a title. Now `applications` has nullable `req_id`
(canonical `<platform>:<board>:<job_id>` ref) and `url_key` (canonical application URL: lowercase host, no `www`,
fragment, trailing slash or tracking params such as `utm_*`, `gh_src`, `ref`; job-identifying params like `gh_jid`
kept). `dedupe.mode = "id_first"` (default): a match on `req_id` or `url_key` is authoritative, and a known id/URL
that matches nothing is NOT a duplicate. Company + title applies only when the candidate has neither identifier, or
against stored rows that have neither (mail-ingested or CSV-imported). `dedupe.mode = "title"` is the legacy
behavior. `store.seen_why(...)` returns the matched reason (`req_id:...`, `url:...`, `title`); `seen()` stays a bool.
Existing databases are migrated on open with `ALTER TABLE ADD COLUMN` (nullable, idempotent) and `req_id` is
backfilled from `ref`. The engine passes `ref=` and `url=` to `seen`/`record`. Revert: `dedupe.mode = "title"`.
Verify: `uv run pytest tests/test_store.py tests/test_search_ranking.py::test_old_db_migrates -q`.

## Amendment 2 (2026-10-22): id-less fallback, company aliases, fuzzy titles
Context: a real run let through two jobs already applied to. (a) Same company and identical title, but the stored
historical row (imported) had no requisition id / url_key while the new posting had one. (b) One employer under two
names (a brand and its parent's long legal name).
Decision:
1. In `id_first` mode an id/URL that matches nothing no longer ends the check: the company+title fallback still runs
   against stored rows that lack both `req_id` and `url_key`. Rows that HAVE a different id remain distinct
   requisitions (two postings with ids and identical titles are never merged). Synthetic refs (`mail:`, `import:`,
   `csv:`, `manual:`, `history:`, `legacy:`) are not requisition ids and never populate `req_id`.
2. Company identity = normalized key (more suffix words: global, technologies, group, holdings, ...), compared
   separator-insensitively so board slugs (`acme-games`, `acmegames`) equal display names, then mapped through
   `dedupe.company_aliases = {"brand": "canonical"}`. Matching resolves stored `company` text at query time, so alias
   edits apply retroactively to history.
3. Title fallback uses token Jaccard with `dedupe.title_threshold` (default 0.6, was fixed 0.75); level words must
   still agree exactly.
4. `seen_why` returns `title~<jaccard> company=<exact|alias>[ legacy-row]` (or `req_id:`/`url:`); the engine puts it in
   the `duplicate` detail.
Consequences: more conservative (more duplicates caught); a lower threshold or a broad alias can merge distinct roles
at one employer; rows with ids are unaffected. Verify: `uv run pytest tests/test_store.py -q`.
Revert: `dedupe.title_threshold = 0.75`, empty `company_aliases`, or `dedupe.mode = "title"`.

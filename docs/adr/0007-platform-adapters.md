# ADR-0007: Platform adapters and submission-path rules

Status: accepted · Date: 2026-10-22

## Context
Most ATS vendors publish unauthenticated JSON for postings, so discovery and screening need no browser
(ADR-0002, ADR-0005). Submission is different: some vendors offer an employer-keyed API, others need an
account or a browser form. A wrong guess here either wastes a browser session or, worse, creates an
account or submits twice.

## Decision
1. **One shared HTTP client** (`platforms/http.py`): a single lazy `httpx.AsyncClient`, every request
   through `HostLimiter`, retries with backoff for idempotent requests only (GET, and POST searches such
   as Workday and Ashby GraphQL), **never** for form submissions (a retry could double-apply).
   HTTP/2 is not enabled (it needs the `h2` dependency, ADR-0004); keep-alive pooling is.
2. **Disk cache** under `<paths.data_dir>/http_cache`, keyed by method+URL+params+body. Fresh entries
   (`platforms.cache_ttl_s`, default 900 s) skip the network; stale ones revalidate with `If-None-Match`;
   a network or 5xx failure serves the stale entry. Authenticated and multipart requests are never
   cached. Files unused for `cache_prune_days` are deleted at first use.
3. **Refs** are `<platform>:<board>:<job_id>`. Workday boards are `<tenant>.<wdN>/<site>` and its job id
   is the path after `/job/`. `matches(url)` is host-based (Greenhouse also `gh_jid=`), and `Services.resolve`
   picks the adapter by ref prefix, else by the first enabled adapter that matches.
4. **Submission path rules** (the contract the apply engine relies on):
   - Greenhouse: API only when `JOB_APPLY_KEY_GREENHOUSE_<BOARD>` exists (HTTP Basic, key as username);
     Lever likewise with `JOB_APPLY_KEY_LEVER_<BOARD>` (sent as the `key` query parameter, stripped from
     error messages). Without a key the result is `needs_browser`, regardless of `dry_run`.
   - Ashby, SmartRecruiters, Workday: always `needs_browser`. Workday requires a candidate account; creating
     one is a human step.
   - `jobspy`: discovery only; `submit` returns `needs_human`.
   - With a key: missing required fields return `needs_human` (nothing sent); `dry_run=True` returns
     `dry_run` and sends nothing; otherwise one POST, `submitted` or `error` with the response text.
   - Keys are read from the environment only (ADR-0003) and never appear in results.
   - Adapters do not evaluate stop phrases or attestations; the policy gate runs before `submit`.
5. **Forms**: Greenhouse (`?questions=true`) and Ashby (public GraphQL `ApiJobPosting`) return real
   schemas. Lever, SmartRecruiters and Workday publish no public schema, so they return the standard
   fields (Lever, SmartRecruiters) or none (Workday) and `submit_via=["browser"]` is the complete path.
   Greenhouse selects map option labels to value ids at submit time.
6. **Text flags** live in the pure module `platforms/textflags.py` (years of experience, sponsorship/OPT,
   citizenship/clearance, graduation window with `grad_window_conflict(windows, "YYYY-MM")`, PhD-only,
   academic, salary range). Flags are computed from list responses whenever the API returns text for free
   (Lever, Ashby, Greenhouse with `content=true`); SmartRecruiters and Workday list responses carry no text,
   so their flags appear in `get_job`. Descriptions are dropped from search rows.
7. **Search** fans out over `(platform, board)` pairs with `bounded_gather`; one failing board becomes an
   `errors` entry, not a failure. Rows are compact, newest first, paginated by `limit`/`cursor`. Boards come
   from the call (`"greenhouse:acme"`, or bare names) or `platforms.boards`. With `apply_policy` a cheap
   pre-filter drops postings contradicting `policy` (sponsorship, citizenship, years, grad window) and reports
   the count as `filtered`; real gating stays in the policy component.
8. **jobspy** is an optional extra (`uv sync --extra jobspy`), imported lazily inside the worker thread; a
   missing install yields a clear error, not an import failure.

## Consequences
- Unknown/undocumented endpoints (Ashby GraphQL, Workday CXS) can change shape; failures surface as
  `AdapterError` with a hint, and fixtures document the expected shape.
- Lever custom questions and SmartRecruiters screening questions are invisible to `get_form`; the browser
  driver reads them from the page.
- Cache growth is bounded only by pruning age, not size.

## Verify / revert
`uv run pytest -q tests/test_platforms.py tests/test_textflags.py tests/test_http.py tests/test_services_search.py`.
Revert: `git revert` the squash commit; `Services.search/get_job/get_form` return to `NotImplementedError`,
and `PlatformsCfg`/the `jobspy` extra disappear with it.

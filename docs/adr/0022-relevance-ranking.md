# ADR-0022: Description-aware relevance ranking

Status: accepted · Date: 2026-10-22

## Context
`search_jobs` required every query term in the title, so roles whose skills appear only in the description were
invisible, and results were ordered only by date.

## Decision
- Retrieval stays cheap: `search.match = "any"` (default) drops the title-only prefilter; `"title"` restores it.
  Location substring and policy gates still run first (hard constraints, deterministic).
- Two-stage ranking in `search/ranking.py` (stdlib only). Stage 1 scores every row on title, location, pay and
  recency. Stage 2 takes the top `search.rank_candidates` (40), fetches their descriptions (bounded by
  `description_concurrency`, the host limiter, an in-process per-ref cache plus the HTTP disk cache), re-applies the
  policy gates to the description-derived flags, and scores again. Rows beyond the budget with no title evidence are
  dropped and counted in `unscanned`.
- Score = weighted sum, normalized to 0-100: BM25 of the query over title (x3) + description; overlap with profile
  skills plus `search.skills`; seniority fit vs `search.seniority`; position in `search.location_order`; posted pay vs
  `policy.salary_floor`; recency (half-life `recency_half_life_days`). All weights are in `search.weights`.
- Each row returns `score` and up to 3 `why` reasons (largest contributions first), e.g. `query: rust (desc)`.
- Optional embedding rerank (`search.embeddings`, default off): query + skills vs title/description through a local
  Ollama `/api/embed` model, blended with `embed_weight`; any failure falls back to the deterministic order.
- `search.rank = false` restores the old newest-first output without `score`/`why`.

## Consequences
Description fetches add up to ~40 requests per uncached search (parallel, cached). A description-only match outside
the top-N by title evidence can be missed; raise `rank_candidates`. Workday/JobSpy rows keep their server-side query.

## Verify / revert
`uv run pytest tests/test_search_ranking.py tests/test_services_search.py -q`. Revert: `search.rank = false` and
`search.match = "title"`, or delete `search/` and the `_rank` stage in `Services.search`.

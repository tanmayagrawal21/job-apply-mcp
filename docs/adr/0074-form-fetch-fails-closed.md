# ADR-0074: A failed form fetch fails closed and is returned as a categorised tool_gap

Status: accepted · Date: 2026-10-28 · Relates to ADR-0031, ADR-0056, ADR-0059, ADR-0067, ADR-0072 · Supersedes the
fetch-failure rule of ADR-0031

## Context
Lever exposes no form schema, so `LeverAdapter.get_form` reads custom-question cards from the hosted apply page
(ADR-0031). When that fetch failed (an HTTP error after the client's bounded retries, a timeout, any other
exception) the adapter returned the standard fields only. That fails open: `missing_required` cannot see the
custom questions it never read, so a plan or an API submit could go ahead as if the form were complete (issue
#127, found by the Control gate on PR #124). A second gap: `engine.plan` and `services.get_form` caught only
`PostingClosed`, so any other `AdapterError` from `get_form` reached the client as a raw tool error, with no status,
reason or category. Batch callers then recorded it as a bare error.

## Decision
- **Lever.** `_apply_page` catches the failure and fails closed. A 404 or 410 raises `PostingClosed` (ADR-0056:
  `skipped`, closed). Any other `HttpError` (after `HttpClient`'s retries), a timeout or transport error, or any
  other exception raises `AdapterError("tool_gap: could not read the Lever apply page (<reason>) ...")`. The reason
  is the HTTP status or the exception type, never the page body.
- **Greenhouse / Ashby.** Both already raised on a non-404 HTTP error, but a timeout or transport error escaped as a
  raw `httpx` exception. They now raise `AdapterError("tool_gap: ...")` for HTTP and transport errors alike, and
  Greenhouse keeps 404 as `PostingClosed`. The Ashby "no posting / shape changed" error is tagged `tool_gap:` too.
  The Ashby browser driver is unchanged.
- **Engine.** `_context` wraps every `AdapterError` from `get_posting` / `get_form` (except `PostingClosed` and
  `NotSupported`) in `FetchGap`, whose `reason` always starts with `tool_gap:`. Adapter resolution errors are
  unchanged.
  - `plan` (and `draft`, which plans first) returns a `needs_human` gate whose reason is that text. The compact
    plan carries `reason` and `category: "tool_gap"`.
  - `submit` returns `SubmitResult(status="needs_human", detail="tool_gap: ...")` before any approval check,
    driver or POST. Nothing is recorded as an application.
  - `apply_batch` rows carry `status: needs_human`, `why`, `reason` and `category: "tool_gap"`, and the batch
    continues. `run_applications` records the item as `needs_human` with that `why`, so its run report puts it in
    the tool_gap handoff category, and the run continues.
- **Services.** `get_form` returns `{"status": "needs_human", "reason", "category": "tool_gap"}` plus the usual
  `cat` and next-step guidance, and no `fields`. `plan_application` / `draft_answers` get the same through the
  plan. `submit_application` gets it through `_submit_row`.
- **Categorisation.** `handoff.categorize` treats a reason that starts with `tool_gap:` as a tool_gap. This check
  runs after the hard-limit wording check and after the result's own structure, so it never downgrades a hard limit
  or an unknown fact. A handoff that is a tool_gap is never handed to the person inside a consent window
  (ADR-0067).

## Amendment (review of #130)
- **Run cap.** In `run_applications` a fetch tool_gap is recorded as `needs_human` but takes no `max_applications`
  slot (`_classify` skips the eligible append when the plan has a `gap`). Before this change such a posting was
  `failed`, and failed postings never took a slot (ADR-0049). The tool's own retryable gap must not crowd out ready
  postings or real hand-offs.
- **Posting APIs.** The Lever postings API (`get_posting`) and the Ashby board read (`get_posting`) also turn a
  timeout or transport error into a `tool_gap:` AdapterError, so a failure on either fetch in `_context` is a
  categorised tool_gap. Before, these escaped as raw `httpx` exceptions.
- **Input errors are not tool_gaps.** A malformed ref, a URL that is not a posting URL for the platform ("not a
  Lever URL") or a board-only URL raises `InvalidRef` (a subclass of `AdapterError`). `_context` lets it through.
  - `plan_application`, `draft_answers` and `get_form` return `{status: "error", error: "invalid_ref", reason,
    next_action: "fix_input"}`. It has no category, because it is not a handoff.
  - `submit` returns `status: "error"` with detail `invalid ref: ...`.
  - `apply_batch` rows are `error` with `error: "invalid_ref"`, and `run_applications` records `failed`.

- **No secrets in echoed refs.** `InvalidRef` sanitises its own message, and every adapter, `BaseAdapter.parse`
  and `Services.resolve` format the ref with `core.secrets.safe_ref`. URLs keep only scheme://host[:port]/path,
  so userinfo, the query string and the fragment are dropped. A non-URL ref loses anything after `?` or `#`.
  Known secrets are passed through `redact()` and the text is truncated. `HttpError` messages use the same
  `safe_url`, so userinfo is dropped as well as the query. `tool_gap_reason` applies `safe_text`, the same rules
  for a whole message.

## Consequences
A transient outage on a Lever apply page now stops that posting with a clear, retryable tool_gap. Before, it was
planned on a partial form. A closed posting is still `skipped`. The status is `needs_human`, the convention for a
handoff. The `category` / `cat` field and the guidance tell the client it is the tool's gap, to retry or report,
and not a question for the owner.

## Verify
1. `uv run pytest -q tests/test_lever_failclosed_127.py`. This covers a 503, a timeout or any exception becoming a
   tool_gap with no POST (the 503 after 3 attempts), 404/410 becoming closed, Greenhouse and Ashby timeouts,
   categorised plan / submit / get_form results, an end-to-end Lever plan and live submit with no POST, and an
   `apply_batch` that records the tool_gap and continues. The amendment adds: a tool_gap takes no run slot, a
   Lever postings-API timeout is a tool_gap, and a malformed Lever URL is an `invalid_ref` error,
   and echoed refs and URLs carry no userinfo, query, fragment or known secret. Then
   `tests/test_lever_cards.py` and the full `uv run pytest -q`.
2. Dry run only: call `get_form` / `plan_application` on a Lever ref while its apply page is unreachable (for
   example, offline). The result must be `status: needs_human`, `category: tool_gap`, with a non-empty `reason`
   and no `fields`.

## Revert
Revert the squash commit of this change. No data or schema migration is needed. Nothing new is stored: tool_gap
results are not recorded as applications, and run-report rows keep the existing `needs_human` status. After a
revert, a failed Lever apply-page fetch silently returns the standard fields again (fail-open), and a
non-closed `AdapterError` from `get_form` reaches the client as a raw tool error.

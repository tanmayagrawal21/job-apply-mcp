# ADR-0025: Confirmation reconciliation and pre-run seeding

Status: accepted · Date: 2026-10-22 · Extends: ADR-0016 (mail), ADR-0021 (submission states)

## Context
A submit can end `submitted`, `unknown`, `submitting` (crash) or `handoff` while the employer's "application received"
email arrives minutes later. Nothing connected the two, so `unknown` stayed stuck until a human looked, and dedupe
only learned of hand-made applications if the user ran `sync_confirmations`.

## Decision
1. **New terminal state `confirmed`** (`apply/approvals.py`). Reachable only through `ApprovalBook.confirm()` from
   `submitting|unknown|submitted|handoff`; it blocks `begin_submit` and no later `set_state`/`resolve` can change it.
   Evidence is the email subject (120 chars), date and sender domain in `detail`; bodies and addresses are never stored.
2. **Schedule.** Whenever a ref enters a reconcilable state the book inserts check times `t0 + d` for each
   `ingest.reconcile_delays_min` (default 2, 10, 60) into table `reconcile` (same sqlite file; idempotent per ref).
3. **Matching** (`ingest/reconcile.py`): all must hold: platform (ref prefix) equals the mail's platform or the mail is
   a generic `employer` one; `company_key` equal; `titles_similar` >= `ingest.reconcile_title_threshold` (0.75); mail
   date within [t0-5min, t0 + `reconcile_window_hours` (72)]; parser confidence >= `ingest.min_confidence`. An email
   that fits several submissions is ambiguous: nothing changes. Store helpers are imported read-only.
4. **Sources.** `imap`/`mbox`: `reconcile_submissions()` fetches the recent window itself. `client` (default): with no
   `messages` it returns a `client_task` asking the agent to fetch recent confirmation summaries and pass them to
   `reconcile_submissions` or `ingest_confirmations` (which now also reconciles). Mail stays read-only.
5. **Outcomes.** Match -> `confirmed` (+ store `submitted`, via email, so dedupe sees it). An `unknown`/`submitting`/
   `handoff` with no match after its last window keeps its state, gets an annotation, and is listed under `unresolved`.
   Reconciliation never resubmits, never touches `failed`, never resolves anything except to `confirmed`.
6. **Visibility.** `control_status` adds `pending_reconciliations {open, due, next_due_min}` (counts only);
   `submit_application` results carry `reconcile_in_min`.
7. **Seeding.** `seed_from_confirmations(since_days=ingest.seed_since_days=180)` ingests confirmation history so dedupe
   works from the store alone (no side files); client source returns a `client_task` first.

## Consequences
The delay schedule is advisory: checks run when `reconcile_submissions`/`ingest_confirmations` is called (no
background daemon, so no credentials held in the server). Late mail beyond the window stays unmatched (extend
`reconcile_window_hours`). Company+title equality is conservative; look-alike roles at one company in the same
window can be reported `ambiguous` rather than guessed.

## Verify / revert
`uv run pytest tests/test_reconcile.py` (fictional .eml fixtures). Revert: remove `ingest/reconcile.py`, the three
tool registrations, the `reconcile` table use and `confirmed` in `approvals.py`; existing rows are unaffected.

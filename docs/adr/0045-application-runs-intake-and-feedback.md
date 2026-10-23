# ADR-0045: Application runs: link intake, run reports and feedback

## Context
Discovery (`search_jobs`) is not the only way a user finds jobs. Users also mail job links to themselves, or have
a list of URLs ready. `apply_batch` takes refs, but it has no notion of where a link came from or of a per-run
size. It writes no report, does not learn from the user between runs, and can stall a batch on inline prose
writing.

## Decision
1. **One tool, several sources.** The `run_applications` MCP tool and `job-apply-mcp run` take any mix of:
   (a) discovery (the existing `search_jobs` path, `intake.discovery` defaults);
   (b) job links the user emailed, from the configured read-only mail source (`intake.mail.source`, default
       `ingest.source`; mbox / .eml directory, IMAP, or `messages` pushed by the client);
   (c) explicit `links` or a file of links (`--links file`, one per line, `#` comments).
   `sources` restricts the set. Candidates are collected in a fixed priority: explicit links, then emailed links,
   then discovery. Duplicate URLs (canonical form, tracking parameters dropped) keep their first, highest-priority
   source.
2. **Mail intake is read-only and fail-closed.** It reuses the ingest readers. IMAP is `EXAMINE` + `SEARCH` +
   `BODY.PEEK` partial fetch, with no `STORE`, `COPY`, `MOVE` or `EXPUNGE`. Bodies are parsed in memory to pull out
   URLs and then dropped. Filters: `sender` and `recipient` (default `"self"`, resolved from
   `intake.mail.self_address`, then the profile contact email, then the IMAP user), `query` (subject or body;
   IMAP `TEXT`), `label` (the IMAP folder or Gmail label to EXAMINE; for mbox, the `X-Gmail-Labels` header), and
   `since` / `since_days`. If `"self"` cannot be resolved, the mail source is skipped with a note; it never falls
   back to reading every message. Redirect wrappers (`google.com/url?q=`, `?url=` and similar) are unwrapped. A
   URL counts as a job link when an enabled adapter claims its host or it matches `intake.mail.job_url_patterns`.
   Processed message keys (the Message-ID, else a hash of sender, date and subject) are stored in the local DB
   (`intake_mail`). A message counts as processed only when every job link in it reached an outcome this run. Links
   deferred by the run cap keep their message unprocessed, so the next run picks them up.
3. **The pipeline is the same for every URL.** Each candidate goes through adapter detection and then
   `engine.plan`, with the usual gates, dedupe and approval issue, followed by `engine.submit`. A URL that no
   enabled adapter handles becomes an `unsupported_site` row carrying the URL. It never uses a plan slot.
4. **The user chooses the run size.** `max_applications` per call; the default is `intake.max_applications` (10)
   or the latest `run_size` feedback, clamped to `intake.max_applications_cap`. At most
   `ceil(max_applications * intake.plan_factor)` candidates are planned. The first `max_applications` that are
   ready in priority order are submitted. The rest are `skipped` with the reason. The existing caps
   (`apply.run_limit`, daily limits, control layer) still apply on top.
5. **Speed.** All candidates are planned concurrently. `apply.plan_workers_deferred` (16) bounds planning while
   prose is deferred, and `apply.plan_workers` bounds it otherwise. Submits run concurrently across hosts, never
   more than one at a time per host (each host's jobs form a queue), under the global `intake.submit_parallel`
   bound. The engine's `concurrency.browser_workers` also applies. A batch run plans with
   `intake.writing_mode = "deferred"` (default) through a context variable (`apply.engine.WRITE_MODE`), so
   `writing.mode` for single plans is unchanged. A job whose only unresolved fields are prose not yet approved is
   `skipped` ("draft_answers, then submit") and never blocks the batch.
6. **The run report is stored.** Items (`intake_items`) and a summary (`intake_runs`) are written at the end of every
   run, including a failed one (`finally`). The categories are submitted, dry_run, confirmed, handed_off (with why,
   for example `awaiting_approval` plus the approval id in human mode), unknown, skipped (with why),
   unsupported_site and failed. `run_report(run_id)` and `job-apply-mcp report` read it back. Before reading, they
   promote rows to `confirmed` when reconciliation (ADR-0025) has confirmed the submission since.
7. **Feedback loop.** `give_feedback(run_id, feedback, ref=None)` / `job-apply-mcp feedback` accepts:
   - structured feedback: `deny_company` / `deny_title` (a regex, or with `ref` that item's exact company or title);
     `answer` (pattern and value); `salary` (`salary_floor`, `default_ask`, `base_salary`); `run_size` (`value` or
     `delta`, resolved and clamped when given);
   - free text, stored as a note.
   Feedback is stored in `intake_feedback`. At the start of every run, all structured feedback is replayed in order
   onto the in-memory `policy` (denylists, salary) and the profile's `answers`. Replay is idempotent. Feedback no
   run has seen yet is listed in `plan_context.applied_feedback`, and pending notes appear in
   `plan_context.feedback_notes`. Both are then marked applied. The user's config and profile files are never
   written. Answers whose pattern concerns authorization, sponsorship, citizenship, demographics, attestations or
   pay are refused through feedback, because a wrong value is a false legal statement. Those are edited in
   `profile.toml`.
8. **Safety is unchanged.** `dry_run` follows the global setting, and a per-run value can only make a run safer (the
   engine's rule). In human approval mode, live runs hand every ready job off with its approval id, as
   `apply_batch` does. Approvals, CAPTCHA, OTP and credential handling are untouched.

## Consequences
- A run is one call with one compact report. Data lands in new tables in `applications.db` (own connection,
  WAL); the existing tables are unchanged.
- Overlays live in the DB, not in the user's files. Removing feedback means deleting its `intake_feedback` row.
  Until the server restarts, a removed overlay stays in memory.
- Two links to the same posting are deduplicated after planning, by posting ref. In dry runs the store records
  nothing, so cross-run duplicates in dry runs are not caught (as before).
- Job-link detection is heuristic. A careers landing page without an id can show up as a row: `unsupported_site`,
  or `failed` if an adapter claims the host but cannot parse the URL.

## Verify
`uv run pytest -q tests/test_intake.py` checks:
- link extraction from fictional .eml and mbox fixtures, covering redirect unwrapping, tracking strip, HTML
  hrefs, the self/since/label/query filters and fail-closed `"self"`;
- the IMAP source is read-only;
- explicit links and a links file;
- `max_applications` is respected;
- priority order, with discovery called only when the pool has room;
- processed mail ids are recorded, and messages with deferred links are kept;
- feedback is applied in the next run and replay is idempotent; sensitive answers are refused;
- `unsupported_site` rows;
- one submit per host with parallelism across hosts;
- deferred prose is skipped without narrative calls;
- human mode hands off; dry_run cannot be relaxed; `confirmed` in reports.

## Revert
Revert the squash commit. Alternatively, remove `_register_intake` from `server.py` and the `run` / `report` /
`feedback` CLI commands. The `intake_*` tables can stay; nothing else reads them.

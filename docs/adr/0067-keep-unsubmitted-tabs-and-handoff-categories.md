# ADR-0067: Filled-but-unsubmitted tabs stay open; every handoff is hard_limit, unknown_fact or tool_gap

Status: accepted · Date: 2026-10-27 · Amends ADR-0036, ADR-0048, ADR-0058

## Context
The owner finishes by hand whatever the tool could not. Before this ADR, only captcha handoff pages
(ADR-0036/0058) stayed open, so a tab that had been filled but was blocked (incomplete, needs_human, a filled
dry run, no confirmation) closed. The person had to start that application again from scratch. Separately, the
owner wants as much automation as possible inside a consent window. Every handoff is a bug unless it hits a
hard limit or a genuinely unknown fact, and tool gaps must be counted so they can be driven down.

## Decision
- **`browser.keep_unsubmitted_open` (default true).** The Playwright driver marks a page as *filled* when it
  starts filling. When the call ends, a filled page whose status is anything other than `submitted`, `skipped`
  or `duplicate` stays open. That covers needs_human, incomplete, handoff, roadblock, a filled dry run, unknown
  or no confirmation, and an error after filling. Pages for closed postings and pages that were never filled
  still close, as do all pages in headless mode. Kept tabs are listed (ref, status, page URL; no secrets) in
  `<data_dir>/kept_tabs.json`, and a tab drops off the list when the person closes it. This never clicks
  anything and changes no gate, so any config layer may set it.
- **Browser lifetime.** Playwright's persistent context is a child of the server process, connected over a pipe,
  so it cannot outlive the process. Making it detachable would require a remote-debugging port, which would
  expose the logged-in profile to every local process, and that would loosen safety. The closest safe option
  works like this: on stdio, after the client disconnects (stdin closes), `serve_stdio` keeps the process, and
  so the browser and any final-click watcher, alive while kept tabs or watchers remain. The limit is
  `browser.linger_after_disconnect_s` (default 3600 s; 0 turns it off). **Limitation:** if the client kills the
  server (SIGTERM or SIGKILL), the browser closes too. `kept_tabs.json` then still lists the URLs, and the
  evidence folders still hold what was filled. The HTTP transport does not stop when a client disconnects.
- **Handoff categories (`core/handoff.py`).** Each handoff status is categorized from its reason:
  - `hard_limit`: a visible captcha, comprehension or anti-automation check, an attestation, legal or terms
    box, a secret, a quota, or the owner's own final-click choice.
  - `unknown_fact`: unresolved required facts, or prose that needs the user's words.
  - `tool_gap`: everything else.

  Inside an active consent window, a step the consent covers is a `tool_gap`, never a reason to hand the work
  to a person. Those steps are approval (`approve_submissions`), a login (`credentials`), an emailed code
  (`otp_unattended`) and a passive widget (`captcha_attempt`). Submit rows carry `cat` and `cat_hint`; a
  `tool_gap` hint inside a consent window says not to hand it to the user. Run reports tag each row with `cat`
  and add `handoff_categories` counts.

- **Guidance uses the category (amends ADR-0059).** `with_guide(row, cat=, window=, state=)`: inside an active
  consent window, a `tool_gap` never gets a next_action from `HUMAN_ACTIONS`. It gets `retry_via_tool`, or
  `reconcile` (run `ingest_confirmations`, never resubmit) when the outcome is unknown. The submission state
  (`submitting`/`unknown`/`handoff`/`awaiting_code`) overrides an `error` status in the guidance, so an unclear
  outcome is never "retry once". Plan, submit and batch rows all go through the same `Services._guided`.
- **#101 review items.** `degraded()` says "nothing was submitted" only when the failure came before dispatch.
  The engine marks the exception with `dispatched`; after dispatch the hint says never resubmit. The resume
  fallback (amends ADR-0061) uses a same-named copy in `<data_dir>/documents` only when the configured file
  exists but the OS denies access, AND the copy's SHA-256 matches the hash recorded the last time the
  configured file was readable (`documents/.resume_hashes.json`). A missing configured resume, or an
  unverifiable copy, fails with an actionable fix. An older copy is never uploaded silently.

- **Review fixes (control + reviewer on 5537e16).** In `categorize`, hard limits are checked first. A detail
  that matches both a hard and a consented pattern is a `hard_limit`. `_HARD` now covers every
  captcha/reCAPTCHA/hCaptcha/Turnstile/Cloudflare check, AI-use and personal-completion attestations, and
  secrets (password, API key, token, credential). A captcha is never a consented `tool_gap`.
  `with_guide` returns `reconcile` whenever the submission state is `submitting`/`unknown`, the status is
  `unknown`, or the detail marks a handoff raised after the submit click ("already submitted once", a code
  prompt). That holds whatever the status or category. `handoff`, `awaiting_code`, `submitting` and `unknown`
  are never steered to a retry, and their `cat_hint` never says to retry. Post-click handoffs already persist
  as state `unknown`, which is in `approvals.BLOCKING` (engine `_apply_code_result` and the submit mapping), so
  `begin_submit` refuses a second click. Batch rows take the submission state from the approvals book. Kept
  tabs with status unknown, roadblock or error carry the note "may already be sent; do not click Submit again".

- **Structure before wording (control review e9e79ae).** `categorize(..., unresolved=, client_tasks=)` checks,
  in order: hard-limit wording; the result's own lists (an attestation task -> `hard_limit`; a journal/profile
  conflict task, any other client task or an unresolved field -> `unknown_fact`); status `incomplete`,
  `needs_input` and `needs_review` -> `unknown_fact`; then wording fallbacks for rows without structure (run
  reports: "N unresolved (K shown)", "still/stayed empty", conflict). An unknown_fact is never steered to a
  retry. A table test takes its strings from the real IntakeRunner, engine, journal overlay and driver code.
- **With ADR-0066.** `captcha_rejected` is a handoff status and always a `hard_limit`: its next_action is
  `tell_user` (never retried), and it is in `NO_RETRY`. Its tab stays open with the note "the site rejected the
  automated submit; the tool will not retry it".

## Consequences
Tabs can pile up only with filled work the person still has to finish. The `tool_gap` count becomes the metric
for automation coverage.

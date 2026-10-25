# ADR-0058: The person makes the final click (`browser.final_click = "human"`)

Status: accepted · Date: 2026-10-25 · Amends ADR-0012, ADR-0036, ADR-0045

## Context
The owner wants unattended preparation, but with the Submit click left to them. Ashby forms protected by reCAPTCHA
Enterprise silently reject or challenge automated clicks, so the agent cannot make that click reliably anyway.

## Decision
- `browser.final_click: "agent" | "human"` (default `"agent"`). `"human"` only tightens, because it never clicks,
  so any config layer may set it. Setting `"agent"` while the trusted user config says `"human"` counts as a
  relaxation (`_RELAXES`), and is IGNORED outside that file.
- In a live run, the driver checks `"human"` right after presubmit validation and before the captcha block. It
  saves evidence (`handoff-final-click`), keeps the tab (`_handoff_pages`), brings it to the front and returns
  `handoff` with "form filled and verified; click Submit in the open tab". It never clicks. In headless mode it
  returns `needs_human` with a hint to set `headless=false`.
- The confirmation watcher is a background task owned by the driver, so it outlives the tool call. It polls the
  tab every `human_click_poll_s` (3 s) for up to `human_click_timeout_s` (900 s), using the same `decide_outcome`
  success detection as the agent-click path. When the success page appears, it saves evidence
  (`confirmed-after-human-click`) and calls the engine's `_human_confirmed`, which records the state as
  `submitted` (the code-step path: approvals state plus the store record). On a timeout, a closed tab or an
  error, the ref stays a handoff (state `unknown`) for reconcile or `ingest_confirmations`. The watcher is
  read-only: it never clicks, types or retries.
- In a live run with `"human"`, `run_applications` processes postings one at a time, across hosts as well. It
  hands off a posting, waits for the confirmation or the timeout, then moves to the next. A confirmed posting is
  reported as `submitted` ("confirmed after human click"). Anything else is `needs_human` with the reason.

## Consequences
The owner gets a queue of filled, verified tabs and does only the clicks. Every ATS, captcha and
reCAPTCHA Enterprise included, goes through the same path, because a person clicks. Dry runs are unchanged.

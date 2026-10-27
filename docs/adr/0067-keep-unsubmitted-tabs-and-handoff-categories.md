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

## Consequences
Tabs can pile up only with filled work the person still has to finish. The `tool_gap` count becomes the metric
for automation coverage. The `next_action` hints from ADR-0059 (PR #101) should take `cat` into account once
both are on main.

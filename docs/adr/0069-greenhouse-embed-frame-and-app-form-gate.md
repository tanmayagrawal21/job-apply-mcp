# ADR-0069: Greenhouse embed iframes on company pages, and never filling a page without the application form

## Context
QA issues #98 and #99: company careers pages (`...?gh_jid=<id>`) embed the Greenhouse application form in an
iframe that Greenhouse's `embed.js` inserts into `<div id="grnhse_app">`, often after the page has loaded. The same
page can carry its own forms (a "Job Alerts" sign-up with First/Last name and Email). ADR-0063 fixed the two known
entry paths: a `gh_jid` URL with a `greenhouse:<board>:<id>` ref is rewritten to the embed, and a board URL that
redirects off greenhouse.io is reopened as the embed. Any other way of landing on such a page (a company URL
without `gh_jid`, a redirect done by script after load, a ref without board/token) still had two failures:
1. The driver filled whichever `<form>` it found on the host page (the job-alert box), because a single host-page
   form is never scoped and the iframe is not part of `page`.
2. The dry run then verified the values it had just typed into that wrong form and reported success.

## Decision
- **Resolve the embed frame first.** For Greenhouse, after the redirect check, when the page is not on
  greenhouse.io the driver looks for `#grnhse_app iframe`, `iframe#grnhse_iframe` or an iframe whose src is a
  Greenhouse embed/job-boards URL. If none is there yet but the page shows one is coming (`gh_jid` in the URL,
  `#grnhse_app`, or the embed script), it waits for it, bounded by `min(browser.timeout_s, 10 s)`. The frame's URL
  (src attribute, else the loaded frame URL) is opened in the same tab, the established pattern from ADR-0063:
  the whole driver then works on the Greenhouse-served form.
- **Navigation allow-list (`dom_rules.greenhouse_frame_target`).** The company page controls the iframe src, so the
  target is checked with the browser's own parse, not only urllib's. Python's `urllib.parse` and the WHATWG parser
  Chromium uses disagree on some inputs: `https://evil.example\@job-boards.greenhouse.io/...` is greenhouse.io to
  urllib but evil.example to the browser (WHATWG reads `\` as `/` in http(s) URLs and strips tab/newline). So:
  1. The raw src is refused if it contains a backslash, any ASCII control or space character (tab, CR, LF, NUL,
     DEL), or any non-ASCII character; its authority (resolved against the page by urllib) is refused if it holds
     userinfo (`@`) or any percent-encoding (`%40`, `%5C`, `%2f`, `%2e`).
  2. The browser resolves the src in the page (`new URL(src, document.baseURI).href`). That string must pass the
     same checks, and its scheme, host and port must equal urllib's reading of the raw src. The browser's string is
     the one that gets opened. A frame without a src (or about:blank) is checked by its loaded frame URL.
  3. Only http(s) on greenhouse.io / `*.greenhouse.io` or the page's own origin is accepted.
  4. When the ref is `greenhouse:<board>:<id>`, the target's `token` or `gh_jid` must equal `<id>`. A page that
     embeds another job's form is refused, because standard keys such as `first_name` would pass the gate below.
- **Application-form gate.** Before anything is typed, the application root (the `data-jam-appform` form, else the
  document) must hold at least one of the posting's own field ids/names (`dom_rules.app_form_missing`, judged only
  when the posting has two or more fields). If none is present, the result is `needs_human` with
  "application form not found ...; nothing filled" and evidence, for a dry run as well as a live run. The gate
  **fails closed**: if the in-page probe raises, the page counts as not holding the form (needs_human, nothing
  filled). Greenhouse only; Lever and Ashby are unchanged.

## Consequences
- Some company pages render their own copy of the application form (field ids such as `form_first_name_7_0_0`, not
  the posting's keys) next to the job-alert form. The gate refuses those pages; the embed rewrites from ADR-0063
  (and this frame resolution) remain the way such postings are filled.
- A job-alert/newsletter form is never filled on such pages, and a dry run never reports success for a form that
  is not the posting's.
- No change to the client fingerprint, user agent or captcha handling (ADR-0036): a captcha inside the embed is
  still handed off as before.
- Cost: on a company page that announces an embed which never arrives, at most 10 s extra before `needs_human`.

## Amendment (2026-10-28): one posting id per frame URL
`greenhouse_frame_target` now refuses a frame URL whose query repeats `token` or `gh_jid` (including empty values),
or carries both with different values, whether or not a ref job id is known. A repeated id used to pass the job-id
check when any one value matched; the host was still allow-listed, so this is hardening, not a security fix.
`token` and `gh_jid` naming the same id are accepted. Tests: `test_frame_target_refuses_ambiguous_job_ids`.

## Verify
- `uv run pytest -q tests/test_greenhouse_embed_98_99.py`. The pure tests cover backslash, userinfo, tab/newline/CR/
  NUL/space, non-ASCII and percent-encoded (`%40`, `%5C`, `%2f`, `%2e`) src variants, a browser resolution naming
  another host or scheme, and the job-id match. The browser fixtures cover:
  - a lazily inserted embed is filled and the job-alert form is not;
  - an embed that never arrives is bounded and gives needs_human with nothing filled;
  - an about:blank slot and an embed of another job are not followed;
  - a failing probe fails closed.
- Dry run (never live) of a company-hosted Greenhouse posting: `submit_application(<ref>, dry_run=true)` reports
  `dry_run` on the `job-boards.greenhouse.io/embed/job_app` URL. A page with no reachable embed reports
  `needs_human` "application form not found" with nothing filled.

## Revert
Remove the `_greenhouse_frame` call and the `_app_form_missing` gate in `PlaywrightDriver._run` (and, optionally,
`greenhouse_frame_target` / `app_form_missing` in `dom_rules.py` and `tests/test_greenhouse_embed_98_99.py`). The
ADR-0063 rewrites (`greenhouse_embed_url`, `greenhouse_offsite_embed`) remain and still cover the known Waymo/Nuro
entry paths. The cost of reverting: a company page reached any other way could again have its job-alert form filled,
and a dry run could report it as success. No stored data or config depends on this ADR.

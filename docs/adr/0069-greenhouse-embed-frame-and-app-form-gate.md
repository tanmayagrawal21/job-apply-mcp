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
  the whole driver then works on the Greenhouse-served form. `dom_rules.greenhouse_frame_target` accepts only
  http(s) URLs on greenhouse.io or the page's own origin, so a company page cannot steer the driver elsewhere.
- **Application-form gate.** Before anything is typed, the application root (the `data-jam-appform` form, else the
  document) must hold at least one of the posting's own field ids/names (`dom_rules.app_form_missing`, judged only
  when the posting has two or more fields). If none is present, the result is `needs_human` with
  "application form not found ...; nothing filled" and evidence, for a dry run as well as a live run. Greenhouse
  only; Lever and Ashby are unchanged.

## Consequences
- Some company pages render their own copy of the application form (field ids such as `form_first_name_7_0_0`, not
  the posting's keys) next to the job-alert form. The gate refuses those pages; the embed rewrites from ADR-0063
  (and this frame resolution) remain the way such postings are filled.
- A job-alert/newsletter form is never filled on such pages, and a dry run never reports success for a form that
  is not the posting's.
- No change to the client fingerprint, user agent or captcha handling (ADR-0036): a captcha inside the embed is
  still handed off as before.
- Cost: on a company page that announces an embed which never arrives, at most 10 s extra before `needs_human`.

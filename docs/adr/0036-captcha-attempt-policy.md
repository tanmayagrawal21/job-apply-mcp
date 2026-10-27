# ADR-0036: `browser.captcha_policy` ("handoff" | "attempt")

Status: accepted · Date: 2026-10-22

## Context
ADR-0012 hands off to a human every page that carries any captcha widget, and always for Ashby. Many of those pages
carry only a passive/invisible widget (reCAPTCHA badge, script, invisible iframe) that never shows the applicant a
challenge, so the handoff adds a manual click for nothing. The repo owner asked for those to work, but not for
visible challenges to be touched.

## Decision
- New setting `browser.captcha_policy`: `"handoff"` (default, unchanged behaviour) or `"attempt"`.
- Under `attempt`, after the form is filled and verified: if the page has only passive/invisible widgets and NO
  visible interactive challenge (visible reCAPTCHA checkbox or image challenge, visible hCaptcha, visible Turnstile),
  the driver clicks Submit **once**, like a person. Otherwise it hands off exactly as before.
- After the click, if a visible challenge appears or the page reports a bot check, the driver STOPS: the tab is left
  open and it returns `roadblock`. The engine maps that to `unknown` and never auto-retries.
- If no confirmation is detected, the existing outcome logic applies (`needs_human`, "do NOT retry"); confirmation
  email reconciliation (ADR-0025) settles it.
- Visibility is decided by reading the HOST page only (iframe `src`, computed visibility, size); frame contents
  are never read.
- `attempt` is a security relaxation (ADR-0021): it is added to `config._RELAXES` and honoured ONLY from the
  trusted, user-owned default config file. Project files, `JOB_APPLY__*` env and `JOB_APPLY_MCP_CONFIG`-selected files
  are ignored for it (logged) and the value stays `handoff`.

## Fail-closed classification (review amendment)
Only a positive allowlist counts as passive: reCAPTCHA badge / invisible iframe / script-only with no checkbox
container, and hCaptcha / Turnstile explicitly declared invisible / interaction-only with no shown frame. Everything
else means HANDOFF: any classifier error, unknown vendors (Arkose/FunCaptcha, GeeTest, DataDome, PerimeterX/HUMAN,
Kasada, AWS WAF, or any iframe src matching captcha|challenge|verify|bot), widgets that are unrendered, zero-size,
off-screen or otherwise inconclusive, frames that cannot be inspected. Open shadow roots and non-vendor child frames
are inspected (vendor frames are never evaluated). Closed shadow roots are covered by the browser rather than page JS: every vendor/generic-pattern frame in `page.frames` has its `<iframe>` element resolved (`frame_element()`) and checked for visibility (unresolvable or hidden non-badge -> HANDOFF; frame contents are never read), and a CDP `DOM.getDocument(depth=-1, pierce=true)` scan looks for vendor class/id markers (unlisted marker or CDP failure -> HANDOFF; vendor iframes' contents are not descended into).
Intentional behaviour change: because unknown/inconclusive widgets now gate on `unsafe` as well as on known widgets, the default `handoff` policy is also stricter than before (pages with an unrecognised bot-check vendor, or a classifier failure, now hand off instead of being clicked).
 Ashby with no
detected widget also hands off under `attempt`. After an attempt click, "no confirmation" returns `roadblock`
(engine `unknown`), never `needs_human`/`handoff`, which could be re-planned and re-entered.

## ABSOLUTE limits (apply to every setting, now and later)
No solving or solver services. No interacting with challenge frames. No stealth or fingerprint plugins. No
webdriver or user-agent spoofing. No automation-flag removal. No token injection. No retries to improve a score.
One click per submission. These are repeated as a comment above the detection code.

## Consequences
Pages with an invisible widget can be submitted unattended when the user opts in. If the site's risk engine
decides to show a challenge, the run stops as a roadblock (status `unknown` until a confirmation email arrives).
Risk-score rejections that show no challenge look like "no confirmation" and are not retried.

## Verify / revert
`uv run pytest tests/test_apply_browser_local.py tests/test_approvals.py -k captcha` (browser tests need local
Chrome and skip otherwise). Revert: remove the setting, its `_RELAXES` entry and the `attempt` branch; the default
behaviour is unchanged.

## Note (2026-10-23): reCAPTCHA loader script marker
Ashby pages load reCAPTCHA with `<script id="recaptchaScript">`. The CDP marker scan flagged that `id` as
`marker:recaptchaScript`, so `captcha_policy=attempt` handed off on a page with only a script-only, passive
reCAPTCHA. That handoff contradicts this ADR. A marker on a `<script>` element (by id or class) now counts as
passive unless it names an unknown vendor (arkose, geetest, datadome and similar). Everything else stays fail-closed
and still hands off:
- a visible challenge;
- a checkbox container;
- a shown iframe;
- any marker on an element that is not a script.

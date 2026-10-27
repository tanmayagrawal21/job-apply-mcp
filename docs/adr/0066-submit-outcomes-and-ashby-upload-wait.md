# ADR-0066: Per-ATS post-submit outcome detection; Ashby upload wait; captcha rejection is terminal

## Context
A read-only study of the shipped Greenhouse, Lever and Ashby front ends (2026-10-27) found:
- **Greenhouse** (job-boards and embed) uses an invisible, score-based reCAPTCHA Enterprise. A low score answers the
  submit POST with `{"code":"captcha-failed","security_code_recipient":...}`, and the page then shows
  `fieldset#email-verification`: the site's own emailed 8-character security code. The driver's old URL/text rule
  read that non-2xx response as "ATS rejected" before the code step rendered, so the existing awaiting-code flow
  (ADR-0026, ADR-0048 otp_unattended) never ran. `captcha-failed` without a recipient and `captcha-retry` mean the
  captcha verdict refused the submission.
- **Ashby**: when Submit is pressed while a file upload or resume autofill is in flight, the app shows a
  short-lived toast "We're updating your application (e.g. uploading files)..." and **sends nothing**. That is very
  likely the 10-23 "silent rejects". A low reCAPTCHA score instead returns a GraphQL error
  `extensions.ashbyErrorType = RECAPTCHA_SCORE_BELOW_THRESHOLD` and replaces the form with a red failure panel
  ("Try these steps").
- **Lever** (hCaptcha) posts only once a token exists. Success is `/thanks` or `[data-qa=msg-submit-success]`.

## Decision
- `apply/outcomes.py` (pure) classifies each click as `success`, `code_requested`, `captcha_rejected`, `blocked`,
  `validation_error`, `not_sent`, or undecided (after the confirmation deadline: `unknown`, reported as before). It
  keys only on stable strings: Greenhouse error `code`s and `#submit-error` text; Ashby GraphQL `FormSubmitSuccess`,
  `blockMessageForCandidateHtml`, `errorMessages`, `RECAPTCHA_SCORE_BELOW_THRESHOLD`, and the success / blocked /
  failure container classes; Lever's `msg-submit-success` and `.error-message`. The network verdict outranks the
  page. Only responses made after THIS click count (`since`), and after a code entry the lingering prompt is not
  read as a new request.
- The recorder keeps up to 8000 characters of each submit response in a private `_cls` field for classification
  only. Evidence files keep the existing 500-character `body` and never contain `_cls`.
- `_conclude` runs the classifier first, then falls back to the old URL/text rule. On Greenhouse/Ashby a "failed"
  from the old rule waits until the deadline so the page can show which failure it is.
  - `code_requested` -> the existing `_await_code` -> `awaiting_code` + `pending_id`. Inside a consent window the
    engine's otp_unattended path (ADR-0048) completes it from mail; outside it the human provides the code. If the
    inputs never render, the result is `roadblock` (engine `unknown`) as before.
  - `captcha_rejected` -> new terminal `SubmitResult.status = "captcha_rejected"` with the reason. The engine records
    it (store) and sets the approval to `handoff` (not received; a human may apply manually). The driver never clicks
    again, never retries, never interacts with a challenge.
  - `blocked` -> `needs_human` with the site's text. `validation_error` -> `needs_human` (`roadblock` under the
    captcha attempt policy), as before.
- **Ashby upload wait.** Before the Submit click the driver waits (cap `upload_settle_s` + 15 s) until no "We're
  updating your application" toast and no uploading / parsing text is shown; if it never settles, Submit is not
  clicked (`needs_human`, "not sent"). If the click still produces the toast and **no submit request was recorded**
  (`not_sent`), the driver waits for the upload to settle and clicks Submit once more. That is not a resubmission:
  the first click sent nothing. A second `not_sent`, any recorded submit request, or an upload that never settles
  stops with `needs_human`.
- Unchanged: the captcha policy (ADR-0036, handoff before the click on Ashby by default), the single-click rule for
  anything that reached the ATS, and the hard limit: the tool never solves, bypasses or evades a captcha.

## Consequences
Greenhouse low-score submissions reach the code step instead of being misreported. Ashby submissions no longer get
lost to an in-flight upload. A captcha refusal is reported plainly as `captcha_rejected` and is never retried
automatically. Lever server-side error wording is inferred and should be confirmed from the first real evidence.

## Verify / revert
`uv run pytest tests/test_submit_outcomes.py`. Revert by reverting this PR; no configuration or schema change.

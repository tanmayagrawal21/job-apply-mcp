# ADR-0068: After a sent submit request, an explicit ATS rejection is `needs_human`; only a missing verdict is `unknown`

## Context
ADR-0066 maps "a submit request was issued but no success confirmation appeared" to `roadblock` (engine state
`unknown`, blocking), because the request may have reached the ATS. Its implementation applied that mapping to
every non-success after an issued request, including an explicit rejection (`network_verdict` = failure, e.g. an
Ashby GraphQL `errors` body). The local browser tests `test_server_error_response_reported` and
`test_no_confirmation_is_needs_human_with_evidence` caught it, but only on machines with Chrome: CI has no browser,
so every local-fixture browser test is skipped there.

## Decision
- After the click, if the ATS answered with an explicit rejection (`outcome == "failed"`), the result is
  `needs_human` "ATS rejected the submission", as before ADR-0066. A rejection is a verdict, not an ambiguity.
- If a submit request was issued and NO verdict came back (no success, no rejection), the result stays `roadblock`
  (engine `unknown`), and the detail now says "outcome unknown: a submit request was sent but no success
  confirmation appeared (it may have been received)" instead of the contradictory "treat as NOT submitted".
- **What counts as an explicit rejection (control review on #108).** `network_verdict` says "failure" for any status
  >= 400 and, outside Ashby, for any body containing words like "required" or "invalid", which is too loose to
  re-open an application. `explicit_rejection` accepts only:
  - an Ashby GraphQL response with a non-empty structured `errors` array and no `FormSubmitSuccess`;
  - a 400/409/422 response whose JSON body carries a non-empty `errors`, `field_errors` or `errorMessages`;
  - the structured per-ATS codes `classify` already handles (Greenhouse `invalid-attributes` / `invalid-attachment`,
    Ashby `FormRender` errors).
  408, 429, 5xx, gateway/timeouts, unparseable bodies and keyword-only bodies are NOT rejections. The request may
  have been processed, so the result is `roadblock` (engine `unknown`, blocking until reconciled). Lever's re-rendered
  error text is an inferred signal, so a Lever `validation_error` after the click is also `roadblock`. Only an
  explicit rejection returns the re-enterable `needs_human` (engine `handoff`).
- If no submit request was issued, the old `needs_human` "treat as NOT submitted" text is unchanged. So is the
  attempt-policy rule (ADR-0036: always `roadblock`).
- The test is renamed to `test_no_confirmation_after_a_sent_request_is_unknown_with_evidence` and asserts the
  ADR-0066 semantics. No assertion is weakened.

## Consequences
An application the ATS explicitly refused is not left blocking as `unknown`. An unanswered submit never reads as
"not submitted". CI still skips the browser fixtures (no Chrome); they must be run locally before merging driver changes.

## Verify / revert
`uv run pytest tests/test_apply_browser_local.py tests/test_submit_outcomes.py` with a local Chrome. Revert by
reverting this PR.

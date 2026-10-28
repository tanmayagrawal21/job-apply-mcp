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
- **What counts as an explicit rejection (control reviews on #108).** `network_verdict` says "failure" for any status
  >= 400 and, outside Ashby, for any body containing words like "required" or "invalid", which is too loose to
  re-open an application. `explicit_rejection` accepts only the ATS's OWN response shape:
  - Ashby: a GraphQL error payload (`errors` is a non-empty list of objects with a string `message`, plus a `data`
    key or `extensions` / `path` / `locations`) without `FormSubmitSuccess`, with status 200, 400 or 422;
  - Greenhouse: its error schema `{"code": "invalid-attributes" | "invalid-attachment", ...}` with status 400 or 422.
  Everything else stays `roadblock` (engine `unknown`, blocking until reconciled), because the request may have been
  processed: 403 (WAF/CDN), 408, 409, 429, 5xx, gateway/timeouts, other ATSs, unparseable bodies, JSON that merely has
  an `errors` key, and keyword-only bodies. Lever's re-rendered error text is an inferred signal, so a Lever
  `validation_error` after the click is also `roadblock`. Only an explicit rejection returns the re-enterable
  `needs_human` (engine `handoff`).
- **Browser-level POST replay (residual risk).** Chrome replays a POST on its own when the server answers 408, or
  when a reused keep-alive socket closes with no response. The test server saw 2 POSTs for one click. Playwright
  interception does not see the replay: probe with Chrome 155 and Playwright 1.63, one click on a `fetch` POST:
  - 408: server 2 POSTs, `page.route` 1 call, `request` events 1;
  - dropped socket: server 2 POSTs, `page.route` 1 call, 1 `requestfailed` (`ERR_EMPTY_RESPONSE`).
  The replay happens inside the network stack, below the CDP Fetch domain, so "allow exactly one submit request and
  abort duplicates" cannot be enforced there. Fulfilling the submit through `route.fetch(max_retries=0)` does avoid
  the replay (1 server POST in both cases), but it was **not adopted**. It would send the application from
  Playwright's own HTTP client instead of the browser, so the ATS would see a different client than the page. That
  is a fingerprint change the captcha rules (ADR-0036) do not allow, and it bypasses browser behaviour such as
  service workers. Consequences:
  - The tool clicks exactly once (tests assert one submit event on the page).
  - A 408, a dropped connection, or any other unanswered submit stays `unknown` and is never re-enterable without
    reconciliation.
  - The ATS's own duplicate-application detection is the backstop for a replay the browser makes.
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

## Amendment (review follow-up on #108): Ashby "your form needs corrections" banner
`outcomes.classify` read the Ashby DOM banner "your form needs corrections" as `validation_error` (re-enterable
`needs_human`) whatever had happened on the network. Ashby's client-side validation shows it before any send, but the
banner can also be on screen beside a submit request that was sent and got no structured verdict (a 502, a dropped
socket, a response still in flight). That broke the rule above. The banner now counts only when no submit response
was recorded after the click (`not subs`) AND no submit request was issued (`not requested`; an untracked count
counts as "maybe sent"), the same guard as the `not_sent` toast. Otherwise only Ashby's own response decides
(`_ashby_net`, then `explicit_rejection`); with no verdict the outcome stays `roadblock` (engine `unknown`).
The reviewer suggested `not subs` alone. `not requested` is added as well because a request that never got a
response leaves `subs` empty. Tests: `test_ashby_dom` and the three `test_corrections_banner_*` browser fixtures.

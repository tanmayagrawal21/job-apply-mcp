# ADR-0039: Radio/checkbox groups, DOM-only consent radios, hydration wait, profile phone country

Status: accepted · Date: 2026-10-22

## Context
The 2026-10-22 dry-run QA round (issues #44, #45, #46) found four driver gaps:
- Ashby radio and checkbox questions were never selected. Their inputs carry entry-scoped names (radios) or the
  option text (checkboxes), so `_locate` found nothing. The only stable handle is the `[data-field-path=<key>]`
  wrapper. A drawn circle or box also covers the real input.
- The required-field scan only looked at *visible* inputs, one at a time. A Greenhouse checkbox group (custom-drawn
  boxes, `aria-required` fieldset, asterisk legend) was invisible to it. A run could therefore report `dry_run`
  ("verified") while a required group was unchecked.
- Ashby renders an SMS consent radio under the phone field that the job API does not list. Nothing surfaced it.
- Greenhouse job-boards is server-rendered (Remix). Inputs exist before React hydrates, and typing or clicking before
  hydration is silently lost. This explains the "flaky" relativity failures (country, location, selects, booleans
  and resume all empty).
Code review also flagged two problems: the phone-widget country was hard-coded to United States, and `_committed()`
without a scope accepted another select's chip.

## Decision
- **Groups.** `_locate` adds `[data-field-path=key] fieldset|[role=radiogroup]|[role=group]` (before label lookup)
  and the bare wrapper (last). Any non-input container that holds radio/checkbox inputs is a *group*. `_choose`,
  `_verify` and `_is_empty` handle groups by each member's own option text (`<label for>`, wrapping label,
  aria-label, value). The question heading is never a candidate. Ticking tries `check()`, then a click on the
  option's label, then a forced check, and reads `checked` back after each step. Verification fails when the group
  is empty or any wanted option is not ticked.
- **Required-group scan.** `_EMPTY_REQUIRED_JS` first walks the groups (`fieldset`, `role=group|radiogroup`, consent
  blocks), using the group's visibility rather than the hidden input's. A group is required when any of these holds:
  - the group or a member is `required` or `aria-required`;
  - the legend or heading contains `*` or `✱`;
  - the legend or heading has a `required` class, or contains an element with one;
  - it is an all-radio consent group.
  An unchecked required group is reported, so the run ends `incomplete` and never `dry_run`. A required group that
  is in the plan is refilled once, as text fields already were.
- **DOM-only consent radios.** These are surfaced by the scan. `_fill_privacy_groups` answers one only when the owner
  opted in (`acknowledge_privacy_notices = true`, which the engine passes as `pref:acknowledge_privacy_notices`).
  The group's full text (not the 80-character label) and its options must also pass ADR-0034's
  `privacy_ack_value`. SMS, marketing and terms consents are excluded there, so they always stay unresolved and the
  run stops `incomplete`.
- **Hydration wait.** After the form renders, the driver polls (cap `browser.hydrate_wait_s`, default 10 s) until
  `document.readyState == "complete"`. On an SSR page (`__remixContext`, `__reactRouterContext`, `__NEXT_DATA__`,
  `[data-reactroot]`) it also waits until the first form control carries React's own `__react*` keys. Reaching the
  cap is not an error; the read-back and the scan still judge the result.
- **Phone country** comes from the profile's `contact.country` (aliases such as US, USA or UK are normalised). An
  option is chosen only on an exact name match after the dial code is stripped, so "United States" never selects
  "United States Minor Outlying Islands". If no country is configured, the widget is left alone; a required empty
  Country field is then reported by the scan.
- **DOM-only voluntary self-ID selects** (EEO questions that the job API does not list) are filled by `_fill_demographics`. This complements the mapper work in #54. A scan of visible, empty react-selects and native selects (required or not) feeds it, and fields in the plan are skipped. Only two answers are ever chosen, both with `best_option` against the offered options: the profile's `[demographics]` value, otherwise the decline option. A required select with no profile value gets the decline option. An optional select with no profile value stays blank. If neither a matching option nor a decline option exists, the select is left empty. The read-back is scoped to the field.
- **`_committed(page, loc, pick)`** always scopes the read-back to the field's own container: the react-select
  shell, or the closest `[data-field-path]`, fieldset, group or field wrapper. With no container the answer is
  `False`.

## Consequences
- An unanswered consent radio group blocks the run even when the site would accept it empty. This fails closed.
- Users without `contact.country` no longer get United States picked silently. Greenhouse then reports Country as a
  required gap.
- SSR pages take up to about 1 s longer to start filling. Non-SSR pages wait only for `readyState`.
- The location answer for Ashby checkbox groups still comes from the planner (#45 point 2). This ADR changes only how
  an answer is applied, not which answer is chosen.

## Verify / revert
`uv run --all-extras pytest tests/test_apply_groups.py tests/test_apply_react_select.py tests/test_apply_engine.py`
runs local fixtures for the Ashby radio and checkbox groups, the SMS consent, privacy-notice opt-in on and off,
Greenhouse required groups (aria-required, asterisk legend, required class), a click that never registers, the DOM-only self-ID selects, the SSR
hydration wait (with a control case that proves early typing is punished), the profile country, and commit scoping.
Revert by reverting this PR. Setting `browser.hydrate_wait_s = 0` disables only the wait.

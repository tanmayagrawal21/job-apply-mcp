# ADR-0028: Commit react-select choices, set phone country first, type Lever urls

## Context
Live Greenhouse dry-runs left react-selects open with an option highlighted but not chosen, Yes/No questions
(rendered as react-selects, typed `boolean`) went through the button path, the required Country (intl-tel) select
was empty, and Lever `urls[GitHub]` could not be filled. Causes: the option click used `opts.get_by_text(...)`,
which searches descendants of the option nodes and so never matched; read-back looked at every select on the page;
hidden duplicate inputs were located first; programmatic `fill` is ignored by controlled inputs.

## Decision
- Scope each select to its own container; click the combobox, type a filter, wait for the menu, CLICK the matching
  `[role=option]` element by index (never Enter), press Escape for multi-selects, and accept the choice only when it
  shows as that select's single-value / multi-value chip. Verify per select, not page-wide.
- `boolean` fields rendered as react-selects are driven through the same path.
- Before typing a phone, choose "United States +1" in the country widget (react-select `#country` or `.iti`), since a
  country change resets the number. Failure reports `phone_country`.
- Prefer visible matches in `_locate`; type `urls[...]` inputs with real key events; URL read-back ignores
  scheme/www/trailing slash. Cookie banners are not counted as validation errors.

- Before any submit click (and in dry-run) scan the DOM for visible required controls (asterisk label, aria-required,
  required; aria-hidden shims ignored; checkbox/radio groups need one member) that are still empty. Any gap returns
  status `incomplete` with the labels; nothing is clicked. Required prose stays a client task upstream.
- EEO selects that exist only in the DOM: the profile value (`demographic:<key>` answers from the engine) or else the
  decline / "I don't wish to answer" option; never an invented demographic.
- Mapper: `located_in_us` is derived from the configured country.

## Consequences
Slightly slower selects (typing + commit check). Only dry-run/local fixtures and live read-back verify it.

## Verify / revert
`uv run pytest tests/test_apply_react_select.py` (fails on the previous code). Revert the PR; no config changes.

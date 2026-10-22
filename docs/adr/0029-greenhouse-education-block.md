# ADR-0029: Fill the Greenhouse Education block (entry 0)

## Context
New Greenhouse job boards render a required Education section (`#school--0`, `#degree--0`, `#discipline--0`
react-selects, `#start-year--0` / `#end-year--0` number inputs, an "Add another" button). The boards API only flags
it (`"education": "education_required"`) and exposes no fields, so the form model never contains it and the run
correctly ended `incomplete`.

## Decision
- Detect from the DOM (`#school--0`), not the API. Profile `[[education]]` already carries school, degree, field,
  start, end, gpa; `Profile.education_rows()` exposes the generic list newest-first (discipline = field, years
  derived from YYYY-MM). The engine passes the first row to the browser driver as `education:0`.
- School: type the name, accept only a normalised-equal option; otherwise an option named "Other" if one exists;
  otherwise leave empty (-> `incomplete`). Never a different school (no fuzzy or containment match).
- Degree: level keyword (Master/Bachelor/Doctor/Associate/M.B.A.) typed as the filter, snapped to the "<Level>'s
  Degree" option; a generic master's is never mapped to the MBA option. Discipline: typed, snapped with best_option.
- Years (and month selects when present) typed and read back; a wrong read-back clears the field.
- Each react-select choice is confirmed by the committed-value check (ADR-0028). Only entry 0; "Add another" is not clicked.

## Consequences
Older degrees are not listed; forms demanding several entries stay `incomplete`. The option lists are fetched live.

## Verify / revert
`uv run pytest tests/test_apply_react_select.py -k education`; dry-run a tastylive Greenhouse job. Revert the PR.

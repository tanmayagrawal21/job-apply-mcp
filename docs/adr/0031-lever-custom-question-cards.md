# ADR-0031: Lever custom-question cards

## Context
Lever's public API exposes no application schema, so `LeverAdapter.get_form` listed only the standard fields. Custom
questions ("cards") render on the hosted apply page as `cards[<uuid>][fieldN]` inputs; the driver's required-field scan
correctly reported `incomplete`, but nothing mapped them, so every Lever job with custom questions was blocked.

## Decision
- `get_form` does one extra GET of `https://jobs.lever.co/<board>/<id>/apply` (rate limited via the shared client, not cached).
- `platforms/lever_cards.py` parses each hidden `cards[<uuid>][baseTemplate]` JSON (fields: type/text/required/options).
  Key = `cards[<uuid>][field<index>]`, the input name. Kinds: text, textarea, dropdown/multiple-choice -> select,
  multiple-select -> multiselect (options kept). If the JSON is absent or malformed, the `li.application-question` DOM
  (label, input type, `required`, radio/checkbox values, `<option>`s) is used.
- Optional standard fields (e.g. GitHub) that the page does not render are dropped so they cannot block a dry run.
- Fields go through the normal mapper: profile/rules answer standard questions, prose becomes a client task.
- Driver: a radio/checkbox group sharing one name is filled by clicking the member whose value matches (best_option) and
  verified by reading `checked` back; selects and text reuse the existing paths.
- Engine: an LLM short answer of "null"/"none"/"n/a" is treated as no answer (it was being typed into optional boxes).
- Any failure fetching the page yields the standard fields only (previous behaviour).

## Consequences
One more request per Lever form. Field order index relies on Lever keeping `fieldN` = position in the card JSON.

## Verify / revert
`uv run pytest tests/test_lever_cards.py tests/test_apply_browser_local.py -k "lever or card"`; dry-run
`lever:acceldata:c12c39cb-68c1-45ae-9a88-601e297957b1`. Revert the PR.

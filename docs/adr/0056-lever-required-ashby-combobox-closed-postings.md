# ADR-0056: Lever required-field markup, Ashby autocomplete commit, closed postings and comprehension checks

Status: accepted · Date: 2026-10-23 · Amends ADR-0036, ADR-0039 · Issues #88, #89, #90

## Context
A QA round on PR #81 found three driver and adapter faults:

1. **Lever (#88).** A dry run of a completely filled Spotify form came back `incomplete`. The ADR-0039 scan
   follows the inverted rule: a visible choice group with nothing selected counts as required unless the API says
   `required:false`. The Lever API has no schema for pronouns, the demographic survey or the consent block, so
   these groups always counted as required. The scan also took the wrong labels. The named `pronouns` set stops
   at its `<ul>`, because the unnamed "Custom" checkbox is a sibling, so the question label was never reached. The
   "Custom" box became a set of its own, labelled "Custom". The future-opportunities checkbox took the
   privacy-policy sentence above it as its label. "What is your location?" is the demographic-survey country
   `<select>`, which is optional. "Current location" was filled.
2. **Ashby (#89).** The location field (`_systemfield_location`) is an `<input role=combobox aria-autocomplete=list>`
   inside `[data-field-path]`. It has no id and no name, and its `<label for>` points at an id that does not exist.
   We observed the live widget with Playwright. Typing sends a debounced `ApiAutocompleteGeoLocation` call. The
   options appear in a floating `[role=listbox]` that `aria-controls` names. Clicking an option sets the input to
   the option text, sets `aria-expanded=false` and saves the value with `ApiSetFormValue`. If the field loses focus
   before an option is picked, the typed text is cleared. The driver only checked for react-select chips, so it
   never saw the commit. Each query and each refill pass waited for its own timeout, which added up to 100 s and
   `fill-failed`.
3. **Closed postings and comprehension checks (#90).** A closed Ashby form whose labels read
   "Name (NOT USED; APPLICATIONS CLOSED)" was treated as live (needs_human, 5 unresolved). Its "Comprehension
   Check" select was offered to the client as a task.

## Decision
- **Optional only when the markup clearly says so (Lever and Ashby, revised after the #91 control review).** The
  unit is the question container: `.application-question` on Lever, the `[data-field-path]` entry on Ashby. A choice
  group counts as optional only when both of these hold:
  1. The container has exactly one label or title of its own. On Lever that is the `.application-label`, or a
     lone checkbox's own `<label>`. On Ashby it is the `.ashby-application-form-question-title`.
  2. The container shows no required signal anywhere.

  On Lever the same rule applies to a native select or a text input. The required signals are:
  - `required`, `aria-required` or `data-required` on the container or on any descendant;
  - any class matching `/(^|[\s_-])required([\s_-]|$)/`, which includes Ashby's hashed `_required_<hash>` and
    Lever's `.required` / `.required-field`;
  - an asterisk anywhere in the container text outside option labels;
  - required wording anywhere in that text: `required`, `mandatory`, `obligatory`,
    `must answer/complete/select/choose/fill/provide`, or a localised marker (pflicht, obligatorio/a,
    obligatoire, obbligatorio, verplicht, obrigatório, 必須, 必填). A bracketed `(required)`, `[required]` or
    `(mandatory)` always counts. There is one exception: "is/are/be required by/under/pursuant to (the)
    (applicable) law/regulation/statute/legislation", as in Ashby's recording-consent notice. That phrase is
    removed before the test, so any other marker in the same text still counts. (This is control review 2 on
    #91, which replaced an earlier start-or-end-only rule.);
  - a `::before` or `::after` asterisk;
  - API `required: true`.

  No title, or more than one title, is ambiguous. The ADR-0039 inverted rule then applies, and an empty group
  blocks. Every other ATS keeps the ADR-0039 rule unchanged. On the live forms, Lever's pronouns, the
  future-opportunities consent and the survey select are optional. So are Ashby's voluntary EEO groups and its
  recording consent. Whether a voluntary EEO question gets an answer stays the mapper's and autonomy's decision.
- **Ashby Yes/No questions.** Ashby keeps a hidden checkbox behind its Yes/No buttons. The direct click now runs
  only when the checkbox is visible. Before, it waited the full 30 s default timeout for each question and then
  fell through to the buttons. That cost about 90 s on Lambda.
- **ARIA autocomplete commit.** `_locate` now also finds `[data-field-path=K] input[role=combobox][aria-autocomplete]`.
  For such an input outside a react-select, `_combo_commit` does the following:
  1. Types each `location_queries()` variant.
  2. Waits until the options in the listbox named by `aria-controls` stop changing.
  3. Picks with `pick_location()`, which needs the city plus every given region or country part. Fields that are
     not locations use an exact match or the best option.
  4. Clicks that option.
  5. Treats the value as committed only when the input text equals the option text and the list has closed.

  `browser.combobox_commit_s` bounds the whole attempt; it defaults to 20 and is capped at 20. If the commit fails,
  the typed text is cleared and the result is False. A second attempt on the same page returns False at once.
  Read-back for these fields requires the committed text exactly.
- **Closed postings are `skipped`.** The markers are a field label with "APPLICATIONS CLOSED", "no longer accepting
  applications" or "(NOT USED", a closed title ("[CLOSED] …"), the same phrases in the description or the page
  text, and a platform 404 or missing posting (`PostingClosed`, a subclass of `AdapterError`, raised by the
  Lever, Greenhouse and Ashby adapters). Planning returns gate `skip` with the reason `closed: …`. `submit` and
  the driver return status `skipped`. `apply_batch` reports `skipped`, and `get_form` adds `status: skipped` and
  the reason. None of these is an owner action.
- **A comprehension check is a visible challenge.** A field labelled "comprehension check", "attention check",
  "are you a human" or similar never reaches the mapper, the LLM or a client task. The gate becomes `needs_human`
  with the reason "visible challenge … never answered or offered as a task". `submit` refuses even an answer the
  caller supplies, and the driver stops before filling if the page shows one. As ADR-0036 requires, the tool
  never solves or studies such a check.

## Consequences
- The Lever false negatives are gone. A real gap is still reported under the question's own label.
- The Ashby location field takes at most 20 s instead of 100 s, and it never leaves uncommitted text. Cohere and
  Harvey use the same widget.
- `skipped` is a new `SubmitResult.status`.
- Lambda dry run: 110 s and `incomplete`/`fill-failed` before; about 15 s and `dry_run` now.
- Dry-run time: the Lever scan no longer runs a false refill pass or a second scan. Most of the remaining time is
  page load, resume parsing and the geocoded location. This ADR leaves those unchanged.

## Amendment (2026-10-28, issue #126): Ashby Yes/No toggles read from the pressed button

### Context
A required Ashby Yes/No question answered "No" was reported as a required field still empty, so the dry run ended
`incomplete` while the screenshot showed No selected. A dry run showed the markup: each `[data-field-path]` entry
holds two `button[aria-pressed][data-option]` options and one `display:none` checkbox. The checkbox is checked only
for "Yes". It stays unchecked for "No" and for "not answered". The required-field scan treated the unchecked
checkbox as "nothing selected", and the boolean read-back used the same checkbox, so an unanswered toggle could
pass a "No" read-back. Clicking the active option again clears it, so a stale read followed by a second click
could also remove an answer. A sibling "Yes" toggle passed because its checkbox was checked.

### Decision
- The required-field scan counts a choice set as answered when a checkbox member is checked OR its own container
  (the checkbox's parent, holding no other question's choice inputs) has a `button[aria-pressed=true]`.
- The boolean read-back uses a checkbox only when it is visible. Otherwise the pressed button decides, and on
  Ashby an unknown state still fails verification.
- The Yes/No question is scoped to its own `[data-field-path="<key>"]` entry when one exists, so a sibling
  question with similar wording is never read or clicked. The label lookup remains the fallback.
- Bounded clicks: no click when the wanted option is already pressed, which covers the late refill pass. After a
  click the state is polled for up to 1.5 s for React to commit, and there are at most two clicks per field.
  Nothing is clicked at submit time.
- Plan and submit agree. In an `incomplete` result, a required field that the plan answered but the page shows
  empty is labelled "planned answer not committed on the page". Empty required fields that the plan never saw
  (DOM-only) are listed in `SubmitResult.unresolved`. When every empty field was planned and none is unresolved,
  the row is a `tool_gap` (ADR-0067) with `next_action: request_feature`, never "ask the user".

### Verify
1. `uv run pytest -q tests/test_ashby_toggle_126.py`. A fictional Ashby-shaped fixture with delayed commits and
   click-to-clear covers these cases: committed No is not empty, unanswered still blocks and never verifies as No,
   the refill does not re-click, a similar sibling is not touched, and the tool_gap or unknown_fact split. Then
   run the full `uv run pytest -q`.
2. Dry run, never live: `submit_application(dry_run=True)` on an Ashby posting with a required Yes/No question
   answered No. Expect `dry_run`, not `incomplete`.

### Revert
Revert the squash commit of this change. No data migration is needed and nothing new is stored. Each part can be
reverted alone: the `pressed()` check in `_EMPTY_REQUIRED_JS`, the visible-only checkbox in `_bool_state`,
`_bool_group` / `_await_pressed` in `_set_boolean`, and `NOT_COMMITTED` in `core/handoff.py` / `core/next_steps.py`
together with the labelling in the `incomplete` result.

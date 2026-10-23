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
- **Lever is judged by its markup.** On Lever (`ats == "lever"`), the `.application-question` container is the unit
  for each question. Its label is the group label: the `.application-label` text without the marker. A choice
  group or native select counts as required only if it carries real required markup or the API marks it required.
  Real markup means `required` or `aria-required` on a member, a `.required` marker (✱) or an asterisk in its own
  `.application-label`, or a `.required-field` wrapper. The scan still fails closed: any one marker, or an API
  `required:true`, makes the question required. A required question with nothing selected still blocks the run.
  Hidden survey sections are not visible, so the scan skips them. Every other ATS keeps the ADR-0039 inverted
  rule unchanged.
- **Ashby is judged by its markup too.** Every Ashby field entry has a `.ashby-application-form-question-title`.
  A required title carries a hashed `_required_<hash>` class, and the asterisk is drawn in CSS. A choice group
  counts as optional when all of these hold: its own title exists, the title has no such class, `required`,
  `aria-required`, asterisk or pseudo-element asterisk, and the API (by `data-field-path` or member name) does
  not mark the field required. Examples are the voluntary EEO groups and the interview-recording consent. A
  group without a title keeps the inverted rule. Whether a voluntary EEO question gets an answer, such as
  "decline to self-identify", stays the mapper's and autonomy's decision. The driver only stops calling these
  groups required.
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

# ADR-0070: EEO selects: privacy opt-in pass-through, Lever EEO block, is_veteran, placeholder honesty

Status: accepted · Date: 2026-10-27 · Amends ADR-0063

## Context
QA issues #97 and #110, re-checked after ADR-0063 with dry runs against the live postings:
1. **Greenhouse (#97).** The demographic-data consent box was never ticked, even with
   `preferences.acknowledge_privacy_notices = true`. The engine passed the opt-in to the driver as
   `profile.lookup(...) is True`, but the profile adapter renders a structured True as `"Yes"`. The opt-in therefore
   never reached the driver. The engine test used a fake that returned a bool, so it did not catch this. The same
   bug also switched off the DOM privacy-notice groups (ADR-0039).
2. **Greenhouse (#97).** The board's veteran select offers only "Yes, I am a veteran" / "No, I am not a veteran" /
   a protected-classification option. A profile `veteran_status` in protected-veteran wording has no equal option.
   ADR-0063 refuses to equate the two (different facts), so a required select stayed empty.
3. **Lever (#110).** The U.S. EEO block uses native `<select name="eeo[gender]">` elements with no id, each wrapped
   in a `<label>` whose title is a `.application-label` div. The DOM self-ID scan skipped elements without an id, so
   nothing was filled. The result was still `dry_run` "filled and verified N fields", because only planned fields
   are verified.
4. Lever's race options carry "(Not Hispanic or Latino)". Under ADR-0063, that counted as an explanatory
   parenthetical, but it actually states a second fact.

## Decision
- **Opt-in pass-through.** `engine._privacy_opt_in` reads `structured:acknowledge_privacy_notices` (the
  `[preferences]` value only, never `[answers]` or a stored answer). It accepts the adapter's `"Yes"` or a bool
  True. Free text never opts in.
- **Lever EEO block.** `_DOM_SELECTS_JS` also returns native selects that have only a `name`. The driver addresses
  them by `select[name=...]`. Their label is taken from the enclosing `.application-question .application-label`.
  `.eeo-section` counts as a self-ID section. Inside it, a native select is optional unless its markup marks it
  required, so Lever's inverted native-select rule does not apply to the voluntary block.
- **`[demographics].is_veteran`** (optional, `"Yes"`/`"No"`, default empty): "ever served, protected or not". It
  answers only a closed table of plain wording. "No" matches "I am not a veteran", "No, I am not a veteran" and,
  because not a veteran implies not a protected veteran, "I am not a protected veteran". "Yes" matches only
  "I am a veteran" / "Yes, I am a veteran" and never a protected classification. `veteran_status` is tried first.
  `is_veteran` is never derived from `veteran_status` (ADR-0063 stands), and it never comes from a stored or persona
  answer. Empty means unknown, and the select is reported (below), so the owner is asked rather than assumed.
- **"(Not Hispanic or Latino)".** An option whose parenthetical says "not Hispanic" / "non-Hispanic" matches the
  owner's race value only when the owner's `hispanic_latino` answer starts with "No". Otherwise the select is not
  answered from the profile.
- **Contractions fold** in demographic matching ("don't" = "do not", "I'm" = "I am", and so on), so
  "No, I don't have a disability" equals "No, I do not have a disability". No other new equivalence is added.
- **Placeholder honesty.** A DOM self-ID select the profile meant to answer (a non-decline value, or `is_veteran`)
  that is still at its placeholder afterwards is returned in `SubmitResult.unresolved` as
  `"<label> (unknown_fact: no option equals [demographics].<field>)"`, or `(tool_gap: the chosen self-ID option did
  not commit)` when the pick did not stick. A dry run then reads "filled and verified N fields; K self-ID select(s)
  left at the placeholder, NOT verified (see unresolved): [...]". A required one still makes the run `incomplete`,
  and that row is an `unknown_fact` by structure (ADR-0067). An optional one does not block a submission, because
  the fields are voluntary, but it is never silent.
- **Demographic consent template tightened.** The data-controller name slot in `is_demographic_consent` can no
  longer hold a method, purpose or processing verb ("AI", "automated", "to", "for", "using", "process", and so on).
  Non-compete, AI-use, export/citizenship, criminal/background, marketing/SMS and other certifications stay outside
  the template and are never ticked. The other ADR-0063 conditions are unchanged: the opt-in, at least one
  non-decline answer committed, a lone box with its own label, and no stop phrase.

## Consequences
Chime-style surveys complete once the profile has an `is_veteran` answer. Until then the dry run names the field and
the key to set. Lever EEO selects are filled. Forms whose wording matches nothing still leave the select blank, but
now say so. `apply/mapper.py` (API-planned EEO fields) is unchanged (until the #119 amendment below).

## Amendment (2026-10-28): served, protected status declined
`is_veteran = "Yes"` says nothing about protected status, so a veteran select that offers only protected-veteran
options (plus a decline) has no option for it. When the owner's `veteran_status` is a decline, or is empty and the
select is required (the decline table as before `is_veteran`), the driver now picks the decline option instead of
leaving the select blank and the run `incomplete`. An optional select with no `veteran_status` stays blank. A
protected classification is never implied from `is_veteran`, and a select with no decline option stays blank.
Tests: `test_served_veteran_*` in `tests/test_eeo_selects_97_110.py`.

## Amendment (2026-10-28): an answered topic is never declined; CC-305 disability family (#119, #120)
QA dry runs: on a Waymo-like survey the race ("race / ethnic group") and veteran ("a veteran or active member of the
United States Armed Forces") selects were committed to "I don't wish to answer" although the profile answers both,
and the dry run said verified. Cause: those labels failed the plain-wording check (`self_id_label_ok`), so the driver
treated the select as having no profile value, and a required select with no value takes the decline table. The
API-planned path (`mapper.demographic_value`) had the same fallback (no exact option -> decline option). On a
MongoDB-like survey the CC-305 disability options matched nothing by exact equality, and the unlabelled hidden
input behind that select was reported as "invalid field (native validation)".
- **Invariant.** When the profile holds a non-decline value for the select's topic (or `is_veteran` on a select
  that is not protected-only), no decline option is ever committed, on the DOM path and the API-planned path. No
  match leaves the field blank and reported: `unknown_fact` when no option equals the value, `tool_gap` when the
  wording is not recognised as the plain self-ID question ("... left blank, never declined"). The decline table
  still answers a required select only when the profile declines or has no value. This supersedes the ADR-0040
  rule that an unmatched value or a non-plain label takes the decline option.
- **Closed label rewrites** (`dom_rules._LABEL_REWRITES`), per topic: "ethnic / racial / race group" is the race
  question; "veteran or (an) active (duty) (service) member (of the United States / U.S. armed forces / military)"
  is the plain veteran question. Nothing else is added; other labels that mention a topic still fail.
- **`is_veteran` table** adds "(Yes/No,) I am (not) a veteran or (an) active member". Never served implies neither;
  served makes the disjunction true. A protected classification is still never implied, and `veteran_status` in
  protected wording alone still matches no plain-veteran option (ADR-0063).
- **#118 fallback tightened.** The served-but-protected-status-declined fallback applies only to a select whose
  every answer option names protected status (`protected_only`). A plain-veteran wording the table misses is an
  unmatched answer (blank, reported), never a decline.
- **"(Not of Hispanic Origin)"** counts as a Hispanic parenthetical, like "(Not Hispanic or Latino)".
- **CC-305 disability family** (`dom_rules.disability_class`): closed patterns for the "yes" answer ("(Yes,) I have
  a disability (, or have had one in the past / or previously had a disability ...)") and the "no" answer ("(No,) I
  do not have a disability (and have not had one in the past)"). A value and an option of the same class are the
  same answer, because the form revisions word one answer differently. This is a deliberate choice: the owner's
  older-revision "No" is taken as the newer "No ... and have not had one in the past". A bare "Yes"/"No" value
  counts only on the status question. Decline options remain the decline table's.
- **Native validation detail** names an unlabelled control by its field (the single combobox or single `<label>`
  of its nearest wrapper), else "unlabelled <tag> type=<type>".
Tests: `tests/test_eeo_decline_119_120.py`.

## Verify / revert
`uv run pytest tests/test_eeo_selects_97_110.py tests/test_driver_fills_97_100.py tests/test_apply_engine.py`.
Revert by reverting this PR. Leaving `is_veteran` unset, or setting `acknowledge_privacy_notices = false`, turns
the respective behaviour off without a code change.

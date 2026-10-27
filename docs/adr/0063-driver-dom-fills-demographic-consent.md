# ADR-0063: Driver DOM fills: off-site Greenhouse redirects, application-form scoping, self-ID survey, demographic-data consent, pronouns

## Context
QA on 2026-10-27 (issues #97-#100, plus a Lever pronouns group) found five gaps in the Playwright driver:
1. `job-boards.greenhouse.io/<board>/jobs/<id>` answers 302 to a company careers site for some boards. The existing
   embed rewrite (`greenhouse_embed_url`) only looked at the *requested* URL, which has no `gh_jid`, so the driver
   filled the company page: a job-alert sign-up box (#98) or nothing at all because the form lives in an iframe (#99).
2. Field lookup by id/label was page-wide, so on a page with two forms the first match (the job-alert box) won (#98).
3. Greenhouse "demographic questions" (custom survey) and the EEOC block are DOM-only. Labels such as
   "I identify my gender as:" were not classified, so a required gender select was declined although the profile
   holds a value; option wording differs from profile wording ("Man" vs "Male", "South Asian (inclusive of ...)"
   vs "South Asian", bare "No" vs "No, I do not have a disability") (#97). A race select that appears only after
   "Are you Hispanic/Latino? No" was never seen, because the DOM was scanned once (#100).
4. A required box "I consent to <company> collecting, storing, and processing my responses to the demographic data
   surveys above" blocks every demographic answer from being submitted (#97).
5. Lever renders pronouns as a checkbox list titled by a sibling `.application-label` (no `<label for>`, no legend).

## Decision
- **Off-site redirect.** `dom_rules.greenhouse_offsite_embed(requested, landed, ref)`: when the requested URL is on
  greenhouse.io and the landed URL is not, the driver reopens
  `job-boards.greenhouse.io/embed/job_app?for=<board>&token=<id>` (the same form, served by Greenhouse). Only a
  redirect that *started* on greenhouse.io triggers it.
- **Application-form scope.** With two or more `<form>`s, the form holding the most API field keys (a file input adds
  weight but never wins alone; a tie marks nothing) is marked `data-jam-appform`. `_locate` skips candidates that
  belong to another form, `_form_text` reads that form, and the required-field scan uses it. Single-form pages are
  unchanged.
- **Self-ID survey.** A DOM select inside `#demographic-section`, `.demographic--container` or `.eeoc__container`
  counts as self-ID even when its label names no topic (no profile key -> the existing rule: required gets the decline
  option, optional stays blank). `dom_demographic_key` classifies labels the mapper's `canonical_key` misses, and only
  when exactly one topic matches (trans/cis wording -> no key). `demographic_pick` keeps exact matching and adds only
  three closed equivalences: gender Male=Man / Female=Woman; an option that is the profile value plus a trailing
  explanatory parenthetical; and, for disability/hispanic only, a profile sentence starting "Yes,"/"No," equals a bare
  "Yes"/"No", and that last one ONLY when the label is the status question itself ("Disability Status", "I have a
  disability", "Are you Hispanic/Latino?"), never a question that mentions the topic (reasonable accommodation, work
  limitations, language skills). The owner's value is used at all only when the label is a plainly-worded self-ID
  question (`mapper.demographic_label_ok`, after removing an answer instruction such as "(please mark all that
  apply)"); any other label gets no profile value, i.e. the pre-existing decline/blank behaviour. `race_detail` is
  tried before `race` (ADR-0055). "I am not a protected veteran" is never equated with
  "No, I am not a veteran" (different facts). The scan repeats up to three passes to catch conditional selects.
- **Demographic-data consent is NOT a privacy-notice acknowledgement.** ADR-0034/0038 cover notices that only inform
  (every word in `_ACK_VOCAB`); this box grants consent to process special-category data, names the company and lists
  processing verbs, so it falls outside that vocabulary by design and the mapper is not changed. It gets its own
  narrow rule, `_fill_demographic_consent`, which ticks a box ONLY when all hold:
  1. `preferences.acknowledge_privacy_notices = true` (the owner's existing opt-in for applicant data processing);
  2. at least one non-decline profile demographic answer was committed on this page (the consent then covers answers
     the owner chose to give; an all-decline survey leaves the box empty);
  3. the box is a lone checkbox in the application form whose OWN label fits `is_demographic_consent`: an optional
     "By checking this box," + "I consent/agree to" + a single data-controller name of at most six words (no "and", "&", "its", "their") + collect / store /
     process / use / retain verbs + "my responses/answers to the (voluntary) demographic / self-identification / EEO /
     diversity survey/questions (above|below)", with nothing else; the name slot may not mention sharing, third
     parties, partners, affiliates, vendors, sale, transfer, marketing, research or any other purpose;
  4. no `policy.stop_phrases` entry appears in the label.
  Otherwise the box stays empty and a required one makes the run `incomplete`, as before. Every tick is recorded in
  each evidence file of that run (`ticked_by_rule`) and logged.
- **Pronouns.** `_fill_pronouns` derives each choice group's label from legend, aria-labelledby/aria-label, the
  question's title element, or the nearest preceding text inside the group; a group with no derivable label is
  recognised by its member name. A group whose label is only a pronouns question (`is_pronoun_label`) is ticked with
  the exact option equal to `[demographics].pronouns`; no value or a decline leaves it unanswered (it is optional).
- The engine passes `demographic:race_detail` to the driver. `apply/mapper.py` is unchanged.

## Consequences
Company-hosted Greenhouse boards are filled in the Greenhouse embed (a captcha there is still handed off, ADR-0036).
Survey forms that previously declined or stayed blank now use the owner's answers; wording outside the tables still
leaves a select blank (for example a veteran survey offering only "No, I am not a veteran" needs an owner value in
that wording). The consent rule is deliberately literal; other consent wording still reaches the owner.

## Verify / revert
`uv run pytest tests/test_driver_fills_97_100.py`. Revert by reverting this PR. Setting
`acknowledge_privacy_notices = false` disables the consent tick without a code change.

# ADR-0073: More authorization / sponsorship / office wordings, and a reason on every unresolved fact field

Status: accepted · Date: 2026-10-28 · Relates to ADR-0038, ADR-0040, ADR-0050, ADR-0053, ADR-0055, ADR-0062, ADR-0067

## Context
In a round-3 dry run (issue #121), about 8 of 33 postings stopped on work-authorization, sponsorship and
on-site questions that the structured `[authorization]` and `[preferences]` facts already answer. Each one came
back as a client task with an empty reason, so neither the client nor the owner could tell which fact or rule was
missing. The issue also said the same wording ("authorized to work in the country where this
position is located") resolved on another posting and suggested the result depended on the batch.

Root cause of that note: the mapper is deterministic. The difference comes from the postings themselves. The
posting where it resolved has a Greenhouse office with a parseable US location, so `job_country()` is `"US"`. The
posting where it did not has the location text "Remote - USA" and the coded office "US - Remote Zone 1". By the
ADR-0040 design, that confirms no country. A question about "the country where this position is located" (or one
that names no country) cannot use `[authorization]` facts until the job's country is confirmed. That rule stays.
What was missing was the reason.

## Decision
Deterministic rules only. No model ever sees these questions (ADR-0040 never-LLM is unchanged).

- **"of any kind / type / sort"** ("an employment visa or sponsorship of any kind") is dropped before the
  vocabulary check, so it asks the same sponsorship question.
- **The US as the adjective of a permit** ("for a US work permit", "U.S. work authorization") still does not
  name the place of work. ADR-0053 and ADR-0062 say so, and `test_explicit_country_question_on_an_unconfirmed_job`
  pins it. "<Company> to sponsor you for a US work permit" is answered on a confirmed home-country job. On an
  unconfirmed job it stays unresolved, and the reason now says why. The short forms count only in capitals and
  only directly before work / employment / immigration / visa / sponsorship / citizenship.
- **Company names in a question**: the posting company, its `dedupe.company_aliases`, and one label word that
  begins or ends a one-word board token ("Quillon" for `getquillon`). That word needs at least 4 letters and at
  least half the token's length, and it must be capitalized wherever it appears (a proper noun). It may not be an authorization word, a country, a US state (or a word of one,
  e.g. "jersey"), a US hub city, a region, or a generic word such as office, remote, team, careers, jobs, global
  or labs, or a scope word such as outside, abroad, overseas, foreign, other or within. Stripping one of those
  would change the question ("work outside the United States"). A company name that contains a scope word
  ("Outside Inc") is never stripped, so its questions stay unresolved.
- **Either-country sponsorship** ("Do you require sponsorship to work in Canada or the United States?"), Yes/No
  only. The answer is Yes when `requires_sponsorship` is true for at least one named country and No when it is
  false for every one. A missing fact leaves the field unresolved, with an `authorization.<CC>.requires_sponsorship`
  gap. The rule never applies to "and", to authorization ("authorized to work in A or B"), to now-only wording,
  or to any extra word.
- **Job country**: a location string that is a confirmed entry with its trailing comma parts dropped
  ("Somerville, MA" beside "Somerville, MA, United States") counts as confirmed. At least two parts (city plus
  region or country) must agree, so a bare city ("Paris" beside "Paris, Texas, United States") never confirms. Every other ADR-0040 rule stays.
- **Office HQ**: "Can you work from our <city> HQ (4 days a week)?" goes to the ADR-0055 office rule. HQ and
  headquarters count as offices, and fewer than five days a week reads `open_to_hybrid` instead of
  `open_to_onsite`. The slot before "work" is a closed list of neutral adverbs: regularly, also, ever,
  possibly, please, consistently, occasionally, sometimes, comfortably, reliably. Any other word ("mind", "hate",
  "not", "hesitate to") leaves the question unresolved.
- **Closed positive templates (amends ADR-0055).** The ADR-0055 open-wording preference and office aliases
  match words around a keyword. That turned negated or aversive questions into the plain preference value:
  "Are you not able to work onsite?" and "Would you mind working from our office?" answered Yes. A word
  blacklist kept leaking ("averse", "unhappy", "if you are not local and do not want to relocate"), so
  `willing_to_relocate`, `open_to_hybrid` and `open_to_onsite`, including the office rule, are now answered only
  when the WHOLE question matches one of the closed positive templates in `_PREF_TEMPLATES` (`pref_template()`):
  - relocation: "(are you | would you be) willing / open / able / comfortable / ready / prepared to relocate
    (to <place>) (for this role) (if hired)", "can you relocate ...", "would you consider relocating ...";
  - hybrid: "... open to / comfortable with (working in) a hybrid schedule / environment / model / policy ...",
    "... willing and able to commit to the hybrid policy ...", "can you work hybrid ...";
  - on site: "... comfortable working onsite / in office / in person (N days a week) (in <place>)",
    "... open to working out of / from our <place> office / HQ (N days a week)", "can you (<adverb>) work from
    our <place> office / HQ (N days a week)" with the closed adverb list.

  These are normalized first: punctuation, case and a trailing "(Yes/No)" hint. `_OFFICE_NEG` is a backstop that
  runs on the WHOLE label before any clause is stripped. Only the "not" of a leading "If you are not ..."
  condition is exempt. The list: not, never, mind, averse, unhappy, uncomfortable, dread, hostile, disinclined,
  decline, rule out, deal-breaker, deal killer, showstopper, quit, walk away, turn down, prefer, tolerate,
  over, ...

  A leading bare condition ("If you are not local / located / based (in | near | to) <place>,") is dropped, and so
  is nothing else. Every place slot, and the condition's place, must resolve to a KNOWN place (`known_places()`):
  - the posting's own location and office strings;
  - the profile's city, region and country;
  - a small gazetteer of US states, countries and about 120 major metros (Bay Area, SF, LA, NYC, DC, ...);
  - a two-letter state code, but only when written in capitals.

  Generic words (the, greater, area, metro, office, HQ, campus, region, downtown) may surround a place, and "or"
  may join two places. "The area" and "this location" refer to the posting's place. Any other words ("Over My
  Dead Body", "Anywhere But New York", "Impossible", "New York Sadly") leave the question unresolved.

  "Are you based in <place>, or ready to relocate?" decides only Yes. Anything else is unresolved (`unknown_fact`,
  "preference wording outside the closed positive templates"). There is no exception for residence-coupled fields.

  Coverage, from instrumenting the non-browser suite on origin/main and on this branch: 14 of the 15 preference
  wordings that main answered still answer. The one that no longer answers is the long ADR-0055 prose question
  "This role is tied to the office location ... Are you currently based in the listed location and able to work
  in person 3 days per week?", and `test_based_here_needs_the_hybrid_preference` now expects it to be unresolved.
  Real-world wordings outside the templates, or with an unknown place, are now the owner's.
- **Citizen fact**: answers only a plain "Are you a US citizen?" (every word is citizen-question wording, and
  there is no "not" or "or"). A
  certification ("I certify that I am a US citizen and will provide proof"), an export-control note or any
  other clause stays with the owner.
- **Reasons**: `MapResult.reasons` maps a field key to `{category, reason}` for every unresolved legal-status,
  place or profile-fact field. The rule that declined gives the reason first. Next comes the profile fact it
  needed ("the profile has no [authorization] requires_sponsorship_now"). The last fallback is a generic
  owner-only reason. The categories follow ADR-0067:
  - `unknown_fact`: a missing fact, an unconfirmed or contradicting country, or export-control / citizenship /
    certification wording.
  - `tool_gap`: the profile has the facts but the wording did not match a rule.

  The plan's client task carries `reason` and `category`. Any other unresolved field gets a generic
  `unknown_fact` reason, so no task has an empty reason.

## Amendment (review follow-up to #122): the schedule decides the fact; conditional office questions
Two reviewer notes on #122 (both also on main before it). The closed positive templates are unchanged; only the
fact a matched hybrid / on-site template reads changes (`schedule_fact()`), and a conditional office question
gets a stricter rule.
- **Five office days a week is on site.** "Are you open to a hybrid schedule 5 days a week?" read
  `open_to_hybrid`. It now reads `open_to_onsite`. On-site templates keep `open_to_onsite` whatever the day
  count. For a hybrid template, a range that reaches five days ("4 or 5 days a week", "3 to 5 days a week") and
  "hybrid ... full time" (full-time employment, or full time in the office?) are ambiguous: they stay unresolved (`unknown_fact`, with a reason). The ADR-0055 office rule
  (`_office_value`) already read five days as on site and is unchanged.
- **"If you are not local to <place>, are you comfortable working onsite?"** dropped the condition and answered
  from `open_to_onsite` alone. A hybrid or on-site template behind that condition is now answered Yes only when
  both the schedule fact and `willing_to_relocate` are true. Anything else is unresolved (`unknown_fact`, with a
  reason that names both facts), and a missing fact is recorded as a profile gap. "If you are not local ..., are
  you willing to relocate?" still reads `willing_to_relocate` alone.

## Consequences
On a dry run of the issue's postings, the wordings above resolve from the structured facts. Some postings have no
confirmed country: "Remote - USA" with a coded office, "Seattle, WA" with the office "US-PERM", or a Lever posting
with no country. On those, place-less, job-place and US-permit questions still need the owner, and each one now
says why. Negation ("without sponsorship", "do you not
require"), now-only and now-or-future handling is unchanged. Legal-status options and export-control wording
still block every authorization rule.

## Verify
0. Amendment (#122 follow-up): `uv run pytest -q tests/test_pref_schedule_followup_122.py` checks each wording
   with Yes, No and mixed fictional profiles (hybrid five days answers from `open_to_onsite`; ranges and
   "full time" stay unresolved; a conditional office question answers Yes only with `willing_to_relocate`).
1. `uv run pytest -q tests/test_auth_wordings_121.py` covers every wording from the issue, the negatives,
   determinism and the plan-level reason. Then run the full `uv run pytest -q`, including
   `tests/test_live_blockers_2.py::test_explicit_country_question_on_an_unconfirmed_job`, which pins the
   ADR-0053/0062 US-permit rule.
2. Dry run, never live: `applywise run --dry-run --source links --link <posting URL>` on postings with these
   wordings. Then use `applywise report` to check that the authorization questions are no longer listed as
   unresolved. Every remaining client task in `plan_application` must have a non-empty `reason` and a `category`.

## Revert
Revert the squash commit of this change. No data migration is needed. Nothing new is stored: `reasons` and the
task `reason` / `category` fields are computed per plan, and profile gaps keep their existing keys. Each rule is
isolated (the rewrite, the either-country rule, company names, the shortened location, office HQ, the citizen
vocabulary, the reasons), so a single rule can also be reverted on its own. To revert only the #122 follow-up
amendment, revert its commit: hybrid templates then read `open_to_hybrid` whatever the day count, and a
conditional office question reads the schedule fact alone. No stored data changes.

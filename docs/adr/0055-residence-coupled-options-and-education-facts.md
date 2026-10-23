# ADR-0055: Residence-coupled options, education facts, U.S.-person status and one unresolved list

Status: accepted · Date: 2026-10-23 · Amends ADR-0038, ADR-0044, ADR-0053 · Issues #85, #86, #87, #89 (mapper part)

## Context
A QA round on the ADR-0053 head found one wrong answer and several fields left open even though the profile states
the facts.

1. **Wrong answer (#85).** A hybrid select offered "Yes, I'm based in this location and able to work from the office
   3 days per week" and "No, I'm not based in this location but willing to relocate". `rule:open_to_hybrid` chose the
   first option for an owner who lives in another state, so the form claimed a residence the owner does not have.
2. **Names and education (#86, #87).** "Legal First and Last Name", "Preferred Last Name", "Degree Type",
   "University or School Attended", "most recent school / degree obtained", "Where have you most recently worked?"
   and "Where was your last internship?" had no rule.
3. **Prior employment (#87).** "Have you worked at X in the past in a Full-time, Part-time, contractor or Intern
   capacity?" did not match a direct-employment form.
4. **Sponsorship preamble (#89).** "X may agree to sponsor individuals to obtain work authorization (such as H-1B, TN
   or O-1 visa status). Will you ... require sponsorship from X to maintain your work authorization in the U.S.?"
   failed the vocabulary check because of the policy sentence and the words "from X".
5. **U.S. person.** No rule answered "Which of the following best describes your U.S. person status?".
6. **Review gate (#87).** A short value supplied by the client for "most recent school" went through the
   client-prose reviewer and failed.
7. **Plan vs. submit (#86).** `submit_application` printed only the first 8 open required fields, so its list left
   out fields that `plan_application` listed.

## Decision
1. **Residence-coupled options (#85).** An option that asserts residence at the posting location ("based in / local
   to / located in this location, area, city or office") is classified as `here`. An option that denies it and says
   the owner is willing to relocate is classified as `relocate`. Everything else is `other`.
   - `residence_match(profile, posting locations)`: **True** only when `[contact]` city + region equal a posting
     location. **False** only when every posting location is confirmed in another region or another country. A bare
     city counts only through a short table of major US cities whose name belongs to one state ("San Francisco",
     "New York"). Otherwise the result is **None**: same region but another city, an unknown bare city, "Remote", or
     no location. A confirmed job country other than the home country also counts as False.
   - `here` is chosen only on True. The office, hybrid or on-site commitment the option makes must also be backed
     by `[preferences]`. `relocate` is chosen only on False, and only with
     `[preferences].willing_to_relocate = true`. In any other case, the other rules see the field with the `here`
     and `relocate` options removed. They may still pick an option that makes no residence claim ("I am open to
     relocating"). If nothing fits, the owner sees the full option list.
   - A yes/no label that asks about residence ("Are you based in the listed location and able to ...?") is answered
     Yes only on a match, and No on a confirmed mismatch. When the label joins residence and relocation with "or"
     ("reside within commuting distance or are you open to relocation"), the answer is Yes if either part is true,
     and No only if both are confirmed false. A conditional ("If you are not local to the area, ...") stays a
     relocation question.
   - These fields are `fact_rule_field`s. The autonomy layer never assumes or infers them.
2. **Names.** "Legal First and Last Name" is the full name. "Preferred Last Name" is `[contact].last_name`.
3. **Education and work facts** (`PROFILE_FACT_KEYS`, never a model and never a persona value):
   - `degree_type` ("Degree Type", "highest level of education"): the highest degree level in `[[education]]`,
     snapped by `pick_degree`. An unrecognised level makes it ambiguous, so the field stays unresolved.
   - `recent_degree`: the latest entry. With "obtained / earned / completed", only an entry whose end date has
     passed counts.
   - `recent_school`: the latest entry's school. "University or School Attended" uses the existing `school` key.
   - `last_internship`: the company of the current or latest `[[work]]` entry with `kind = "internship"` (or an
     "Intern" title, when no kind is set).
   - "Where have you most recently worked?" maps to `recent_company`.
   - A start quarter has no profile fact and stays with the owner. A missing fact records a `profile_gaps` entry.
4. **Prior employment.** The direct-employment form also accepts "in the past", "at any time", and "in a
   <full-time / part-time / contractor / intern ...> capacity" listing two or more capacities. "In any capacity", "as
   a <role>" and the "or an X affiliate" form stay unresolved (ADR-0038/0053 safety cases are unchanged).
5. **Sponsorship preamble.** On a confirmed home-country job, a leading sentence that is exactly "<company>
   may/will/can (agree to) sponsor individuals (to obtain work authorization)" is dropped. Its example clause may list
   only that country's visa types. "Sponsorship from <company>" then reads as "by <company>". Answers still come only
   from structured `[authorization]` facts. Non-home and unconfirmed jobs stay unresolved.
6. **U.S. person.** A new `us_person` key is answered only when every word of the label is U.S.-person / export-control
   definition wording. The source is `[authorization].us_person`, else `policy.us_person` when the owner's config
   sets it explicitly (the default `False` is not a fact). If neither is set, the owner's own `[answers]` value for
   that exact wording applies, or the field becomes a gap (`authorization.us_person`).
   - For a non-U.S. person, a "None of the above; I am a citizen of a different country" option is chosen only when
     the citizenship is known, is not the US, and no option names that country.
   - A label that also asks about work authorization stays unresolved.
7. **Short facts skip the prose reviewer.** A client value for a recognised short-fact field (name, school, degree,
   employer, title, city, country, zip) skips the client-prose reviewer of ADR-0044 when it is one short line: at most
   100 characters and 12 words, one sentence. The deterministic text guard (charset, attestation, injection) still
   runs.
8. **Race detail (owner request).** `[demographics].race_detail` (optional, default "") holds a finer race value
   ("Irish") next to `race_ethnicity` ("White"). A race/ethnicity select or radio takes the option that
   `exact_option` matches to `race_detail`. If none matches, it takes the `race_ethnicity` (EEO-1) option, as before.
   A broader or narrower option is never chosen ("White" for "Irish", or "Northern Irish"). A declined
   `race_ethnicity` still declines. The value is used only on the EEO path (ADR-0040) and is never sent to a model.
9. **One unresolved list.** `submit_application` lists open fields in full and in the plan's order, from the same
   mapping call as its answers (the cached or fresh plan). It also returns them as `unresolved`. Blocking still
   requires a required field without an answer.

## Consequences
The Harvey hybrid question gets the relocate option when the owner lives in another state. It is unresolved (never
"based here") when residence cannot be compared. More education, name and prior-employment fields come from the
profile. The Stripe "or a Stripe affiliate" question stays with the owner, as ADR-0053 decided. A Lambda posting whose
location is "Austin, TX - Data Center" has no confirmed job country, so its sponsorship question still stays
unresolved (ADR-0040 deny-by-default).

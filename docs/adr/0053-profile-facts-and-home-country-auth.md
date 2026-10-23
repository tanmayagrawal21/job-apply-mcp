# ADR-0053: Home-country authorization recognition and profile-fact answers

Status: accepted · Date: 2026-10-23 · Amends ADR-0038, ADR-0040, ADR-0050 · Issues #76, #77

## Context
A QA round over 12 real postings reached no `dry_run_ok`. Of 60 unresolved fields, 11 asked about US sponsorship or
authorization and 19 asked about prior employment, the current employer or title, or residence. The profile already
holds these facts. Root causes:

1. **The job country was not confirmed.** Some Greenhouse offices have no `location`, only a `name` such as
   "United States". ADR-0040 counted any office without a location as unconfirmed. So "the country where the job is
   located" (Airbnb US) had no country and stayed unresolved.
2. **Some wordings were not recognised.** "retain or extend your work authorization" failed the strict vocabulary
   check. A misspelling such as "require sponorship" (Cohere) did not read as a sponsorship question at all. That
   question then took a flat `[answers]` value (`src: profile`), which ADR-0050 forbids.
3. **Prior employment.** "Have you ever been *previously* employed by X?" did not match the direct-employment form.
   "X or an X affiliate" was rejected outright. "Have you worked with X?" (X = the posting company) had no
   canonical key, so it could reach the persona or skill inference.
4. **Employer, title and residence had no rule.** No rule covered "current or previous employer/title", "the country
   where you currently reside", "what city and state do you reside" or "are you located in X".

## Decision
1. **Greenhouse office names.** An office with no location whose `name` is exactly a full country name
   (`country_iso(name, allow_codes=False)`) gives that country. Labels such as "US-PERM" or "NYC" stay
   `UNCONFIRMED`. `job_country()` is unchanged: every location string must still agree.
2. **Recognition, not sources (authorization).** Auth answers still come only from structured `[authorization]` /
   `[authorization.<CC>]` facts, on a home or confirmed country, exactly as in ADR-0050.
   - `normalize()` reads misspellings of "sponsor" ("sponorship", "sponsership", "sponser") as "sponsor", so such a
     question is always recognised as an authorization question, and a flat answer never applies.
   - "retain" and "extend" join the generic authorization vocabulary.
   - More "job country" phrasings count as the confirmed job country: "the country where this role will be based",
     "the role's country", "the country of this position".
   - On a confirmed home-country job, a question that also names that country has the posting company's name removed
     before the vocabulary check. This already applied to questions that name no country.
   - A question that names the US only as the country of a permit or visa ("a US work visa") still needs a confirmed
     US job (ADR-0040 is unchanged). Non-home and unconfirmed jobs stay unresolved, and the #54/#73 safeguards are
     untouched.
   - A sponsorship select with separate "now" and "in the future" Yes options is never guessed when sponsorship is
     required.
3. **Prior employment from work history.** This covers the `[[work]]` companies plus the resume experience
   organisations.
   - "been previously/formerly employed by X" is a direct-employment form.
   - A label about the posting company (or a configured alias) is treated as `prior_employee` before any persona,
     inference or flat lookup, even when no alias matched ("Have you worked with Acme?"). The answer is Yes only if
     the company is in the work history, otherwise No.
   - "X or an X affiliate/subsidiary" is Yes when X is in the history. Otherwise it stays unresolved, because a No
     would also deny affiliates the profile cannot know about.
   - With no work history at all, the field stays unresolved and becomes a profile gap (`work`).
   - When a choice field has several Yes options ("Yes - Intern" / "Yes - Full Time"), the rule does not pick one.
4. **Profile-fact keys (`PROFILE_FACT_KEYS`).** They are answered only from `[contact]` / `[[work]]`. They are
   never sent to a model and never take a persona value.
   - `current_company` / `current_title`: the single `[[work]]` entry with no end date.
   - `recent_company` / `recent_title` ("current or previous"): that entry, or else the entry with the latest end
     date.
   - `country` (of residence): `[contact].country`. A select takes the one option that `country_iso` maps to the same
     ISO-2 code ("US", "United States", "UK").
   - `residence_city_state`: "City, Region" from `[contact]`. "If located in the US, ..." is answered only when the
     contact country is the US.
   - `located_in_place` ("are you located in X") is Yes or No when X is a country, or a US state named in full (not
     "New York" or "Washington", which are also cities). It is Yes when X is the owner's own city.
   - `located_near` ("within N miles of X") is Yes only when X is the owner's own city. There is no geocoder, so
     anything else is not computable. Any other place stays unresolved; only the owner's own `[answers]` pattern for
     that exact wording may apply.
   - Relocation, hybrid and onsite questions (yes/no style only) use `[preferences]` when they are set.
   - A missing fact leaves the field unresolved and adds `profile_gaps` entry `{"fact": "<section.field>",
     "profile_fact": true}`. The engine records it as `profile.<section.field>`.

## Consequences
More real forms finish without the owner when the facts exist. Every new answer is a fact the profile states.
Wordings the rules cannot compare exactly stay unresolved. Stripe's US postings stay unresolved on the
"US work permit" sponsorship question: their only office is "US-PERM", so the job country is not confirmed. That
case is ADR-0040's deny-by-default, not a recognition gap. "Current company" with no open-ended `[[work]]` entry
no longer falls back to the first entry; it is now a `work.current` gap.

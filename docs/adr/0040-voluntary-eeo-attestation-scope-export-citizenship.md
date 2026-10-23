# ADR-0040: Voluntary self-identification, attestation-gate scope, export-control citizenship countries

## Context
A QA round (issues #47, #48, #51) found:
1. Voluntary EEO / self-identification questions were silently blank. The `[demographics]` model had no field for the
   separate "Are you Hispanic or Latino?" question (it reused `race_ethnicity`, and a `hispanic_latino` key in a
   profile was ignored), a value without an exact option fell back to declining, and optional fields that stayed
   unresolved were dropped from the plan without a trace.
2. The stop-phrase gate matched raw substrings over the posting text plus all form labels joined by spaces, so
   "no ai" matched "no aim", "casino airline", two adjacent labels, or "No AI experience required".
3. An export-control "countries of citizenship" question was never prefilled although the profile holds the fact.
4. `prior_employee` (ADR-0038) lacked public-sector qualifier words.

## Decision
- **Demographics.** `Demographics` gains `hispanic_latino` (default: decline) and `gender_if_required` (used only for
  a required gender question; empty = `gender`). `mapper.demographic_value`:
  - profile says decline -> the decline option (or nothing when there is none);
  - a real value -> the exact option (`exact_option`) only when every label word is generic self-identification
    wording or a topic word of that same key (`demographic_label_ok`, deny-by-default). Anything else ("Gender of your
    emergency contact", "veteran-owned business", "Race and gender", "active member of the Armed Forces") falls back
    to the decline option if one exists, else stays blank. Never a nearest or guessed option.
  - Optional self-identification fields left blank (`_DEMOGRAPHIC_LABEL` or `_VOLUNTARY_LABEL`: impairment, armed
    forces, voluntary, self-identification, ...) are returned as `MapResult.skipped_voluntary`. The engine lists them
    in the plan output as `skipped_voluntary` (key -> label) and never sends them to the LLM tier.
- **Attestation gate.** `policy.gates.stop_phrase_hit` matches a stop phrase as whole words within one segment (one
  field line or one sentence). `PolicyGates` passes one line per field (label and its options joined by " | "), so a
  phrase never spans two fields or two options. A hit followed by requirement wording (experience, background,
  knowledge, skills, degree, required, needed, AI/ML ...) is a job requirement, not an attestation. Every other hit
  still returns `needs_human`; the reason now quotes the field text. `ApplyEngine._stop_hits` uses the same matcher on
  each label and option. Truthfulness certifications are unchanged: `_ATTEST_LABEL` makes them `needs_human` client
  tasks, never answered automatically.
- **Export-control citizenship.** New canonical key `citizenship_countries` (before `us_citizen`). It is answered from
  the profile's citizenship fact (`[authorization].citizenship`, else the owner's own "country of citizenship"
  answer) only when every label word is in `_CIT_VOCAB` (country/countries/citizenship and generic request words).
  Selects need an exact option for every listed country. Labels that also ask for permanent residence, dates, former or
  dual citizenship, someone else's citizenship, "U.S. person" or ITAR stay unresolved; when the profile has the fact
  the client task carries it as `known.citizenship` so the owner only adds the rest. "Are you a U.S. person under
  ITAR?" is never derived from the citizenship fact; only an explicit `[answers]` entry answers it.
- **Prior employee.** `_CO_QUALIFIERS` adds county, city, municipal(ity), police, township, borough, sheriff,
  university and usaf; `_CO_QUALIFIER_PHRASES` adds "armed forces", "transit authority" and "school district".

## Amendment 1 (review of 1fed829, QA round 3: #56, #57, #58)
- **EEO isolation.** `map_fields` classifies EEO first (`eeo_class`: any word form of gender/sex, Hispanic/Latino,
  race/races/racial/ethnic*, veteran(s)/military/armed forces, disability/disabilities/impairment, sexual
  orientation/LGBT, pronoun(s), or generic EEO/self-identification wording). An EEO field is answered only by
  `demographic_value` (the `[demographics]` value or the decline option); us_citizen, privacy_ack, authorization,
  prior_employee and saved `[answers]` never see it. `eeo_option_ok` checks that a chosen option belongs to the field's
  own domain (category words and values such as "Asian" or "Woman"; race and Hispanic share one domain) and that a bare
  Yes/No never answers a select that has category options. Outside the EEO path an answer whose option names a
  demographic category is dropped to unresolved, and `privacy_ack_value` refuses any field whose label or options
  name a category. EEO fields never go to the LLM.
- **Option matching.** `best_option` containment is word-bounded both ways (no "Female" -> "Male"), and values or
  options that name a demographic category use `exact_option` only, with an explicit synonym table (man/male,
  woman/female, non-binary). No match -> unresolved.
- **Attestation gate.** Required markers ("*", "(required)", "required", "needed", "mandatory") are stripped before
  matching and are never an exemption. The only exemptions are: (1) whole-word, per-segment matching (no substrings,
  no spans across fields or options); (2) in posting text only, a "no ..." phrase followed by a requirement noun
  (experience, background, knowledge, expertise, skills, degree). Periods inside abbreviations ("A.I.", "U.S.") do not
  end a segment.
- **US authorization/sponsorship (#57).** `auth_core` drops an example clause (e.g./i.e./such as/like/for example/
  including) that lists only US visa types, treats text after the question mark as a note that may contain only
  authorization/visa/export wording and no place, and, when the posting location is US-only (`location_is_us`), reads
  "the country where the job is located", "this country" and "this location" as the US. The rest of ADR-0038's
  allow-list is unchanged. `location_is_us` is deny-by-default: every part of the posting location must say US/USA/
  United States, name a full US state (not "Georgia"), or be "City, ST" with a state code that is not also an
  ISO-3166 country code or another country's subdivision code (Australia WA, India, Italian provinces, Brazil); an
  ambiguous code (CA, IN, DE, LA, WA, MI, ...) counts only with a city from a small explicit US city table, and
  well-known shared city names (Perth, Melbourne, Sydney, London, Paris, Birmingham, Cambridge, Hamilton, Victoria,
  Vancouver, Kingston) need an explicit US marker. No location, "Remote" or any non-US part -> the "this country" wording stays unresolved.
- **Country-general authorization (Amendment 2).** The tool is not US-centred.
  - *Profile.* `[authorization.<ISO-2>]` tables hold `work_authorized`, `requires_sponsorship` and `citizen` per
    country. The flat `[authorization]` keys are an alias for `home_country` (default "US"), so old profiles behave
    as before.
  - *Job country.* `job_country(location, countries, locations) -> ISO-2 | None` is one strict conjunctive rule for any
    country: (a) the structured adapter countries are exactly one ISO-2 code, with every location entry parsed (Lever
    `country` + `allLocations`, Ashby `addressCountry` of every location, Greenhouse every office location ending in a
    full country name); a non-dict, malformed, country-less or additional entry is "UNCONFIRMED"; (b) every location
    string is in that country by `location_in_country`; (c) no location string names a region or several places
    (americas, latam, emea, apac, europe, global, worldwide, international, anywhere, multiple, and, also, plus,
    possible, "or", ";", "/", "&", "+"). No text fallback; a contradiction gives None. `job_is_us` is an alias.
  - *Locations.* `location_in_country(text, code)` is data-driven (`apply/places.py`): country names, subdivision
    names and codes (US, CA, AU, IN, GB), ambiguous codes derived from ISO-3166 and the other countries' codes plus
    explicit extras, a small city table for ambiguous codes, and shared city names with their usual country. Only the
    last comma component decides; "(US)"/"(Canada)" become a component, an allowlisted note ("(HQ)", "(Hybrid)",
    time zones) is dropped, and any other parenthetical is its own location that must also be in the country.
    `location_is_us` is an alias.
  - *Answering.* A question that names one country uses that country's facts (else the owner's `[answers]` pattern
    naming it); a place-less question, or "this country", uses the job's confirmed country. The allow-list runs with
    that country's name and visa lexicon (`VISA_LEXICON`: US H-1B/OPT/TN..., CA LMIA/PGWP..., GB Skilled Worker..., AU
    482/485...), so a US visa example never makes a Canadian question answerable. Several countries, a region (EU),
    an unknown job country or missing facts leave the field unresolved; missing facts are recorded as
    `profile_gaps` ({country, fact, key}) in the plan output for the profile builder. Export control and "U.S. person"
    remain legal, never-LLM and unresolved by default.
- **Short-answer allowlist (final, fail-closed).** The short-answer model only sees a label that FULLY matches one of a few anchored templates (`_LLM_SAFE_TEMPLATES`: which tools/languages, favourite tool, how did you hear about us, GitHub/portfolio link, notice period, earliest start date); one allowlisted word inside another question never qualifies, and "experience with X" is not a template. Earlier scope (`llm_safe_short`):
  tools, technologies, skills, years of experience with something, notice period or start availability, links or
  portfolio, referral source. Everything else (criminal record, police cautions, residence statuses such as
  pre-settled status or Stamp 4, working hours, driving licence, ...) stays with the owner. `never_llm` and the
  engine's sensitive checks remain as defence in depth. US citizenship comes from the US facts: the flat `citizen` key
  counts only when `home_country` is US, otherwise `[authorization.US].citizen`.
- **With the gap collector (ADR-0047).** A per-country authorization gap is recorded as
  `authorization.<ISO-2>.<fact>`; that field is not passed to the collector as the unscoped home-country key, so an
  owner's answer to a Canadian question can never become a US fact. An authorization/sponsorship field whose country
  is NOT confirmed (place-less on an unconfirmed job, several countries, a region, a contradiction) is recorded under no
  key at all: it stays unresolved and the posting is a handoff, so an answer given in that context is never reused.
- **Free-text EEO fields.** A free-text EEO box is blank by default, whatever the profile says. The only exception is
  a REQUIRED free-text gender field, filled from `demographics.gender_if_required` (never from `gender`, never when
  optional, never a decline value). Select fields keep the exact-option / decline rule.
- **Legal-status block.** The authorization and sponsorship rules never answer a field whose label, help text or
  options mention ITAR/EAR, "U.S. person", export control, citizen/citizenship, permanent resident, green card,
  security clearance or deemed export (`auth_legal_blocked`); such a field stays with the owner and never goes to an
  LLM. This also leaves a sponsorship question with an ITAR note unresolved.
- **Never-LLM questions (#58 and export non-determinism).** `never_llm` (label or options) covers export control,
  ITAR/EAR, "U.S. person", citizenship, nationality, immigration, visa, sponsorship, authorization, clearance and
  criminal history, plus residence, location, relocation, commute, on-site, in-office, hybrid, remote, time zone and
  address; plus arrest, visa types (F-1, H-1B, OPT, CPT, TN, J-1, O-1, L-1, EAD), nationals, passport, 1324b,
  debarment, "lawful(ly)" and work rights; and, for short answers, office, metro, near, where, move/moving, HQ,
  "work in/from", area and region. The engine sends these fields to neither the short-field model nor the writer; they are answered by
  deterministic rules or become owner tasks.
- **Residence.** "Do you (currently) reside/live/are you located/based in the (continental) United States?" is answered
  from the profile country; "continental"/"contiguous" also needs `contact.region` (Alaska, Hawaii and territories ->
  No; missing -> unresolved). Any other wording stays unresolved.
- **Privacy-notice acknowledgement (#56).** The acknowledgement vocabulary adds plain words such as "below",
  "receipt", "linked", "attached" and "required". The posting company's full name is removed only as an exact
  phrase that owns the notice ("Acme's Privacy Notice", "Acme Applicant Privacy Policy"); its words never satisfy or
  erase other content, and a privacy / data-protection notice noun must remain after removal. The opt-in,
  the exclusion list, the sensitive-topic checks and the EEO refusal are unchanged.

## Consequences
More voluntary EEO answers from the owner's stated values, visible blanks instead of silent ones, fewer false
attestation stops, and one more prefilled export-control field. Wording outside the vocabularies stays unresolved
(or declines), so the vocabularies may need generic additions over time. Greenhouse self-identification selects that
exist only in the page DOM are still filled by the browser driver's required-only pass; filling optional DOM-only
blocks is a driver change outside this ADR.

## Verify / revert
`uv run pytest tests/test_qa_round3.py tests/test_gates.py tests/test_mapper_safety.py tests/test_privacy_ack.py tests/test_apply_dogfood.py`.
Revert by reverting this PR. The two new `[demographics]` keys have defaults, so existing profiles keep loading.

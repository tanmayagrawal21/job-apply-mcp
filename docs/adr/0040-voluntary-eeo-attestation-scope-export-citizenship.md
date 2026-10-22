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
- **Confirmed-US job.** The `[authorization]` facts are US facts. Unless the job is confirmed in the US, an
  authorization/sponsorship question is answered only when the label itself names the US ("US", "U.S.", "United
  States", or a US-only visa type such as H-1B, OPT, CPT, E-3); a place-less "Will you require sponsorship?" stays
  unresolved. Confirmation (`job_is_us`) uses STRUCTURED adapter metadata first: `Posting.countries` from Lever
  `country`, Ashby `address.postalAddress.addressCountry` (primary and secondary locations) and Greenhouse offices
  whose location ends in "United States"; exactly ["US"] (and no foreign country in the text) confirms. A location without a country among several (Lever allLocations, Ashby secondaryLocations, Greenhouse offices) makes the set unconfirmed, never US. Only when the
  adapter gives no country does the free-text heuristic decide, and there only the LAST comma component counts ("US"
  marker, full state name, or "City, ST"); "(US)" parentheticals are kept as a US marker, others are dropped.
- **Legal-status block.** The authorization and sponsorship rules never answer a field whose label, help text or
  options mention ITAR/EAR, "U.S. person", export control, citizen/citizenship, permanent resident, green card,
  security clearance or deemed export (`auth_legal_blocked`); such a field stays with the owner and never goes to an
  LLM. This also leaves a sponsorship question with an ITAR note unresolved.
- **Never-LLM questions (#58 and export non-determinism).** `never_llm` (label or options) covers export control,
  ITAR/EAR, "U.S. person", citizenship, nationality, immigration, visa, sponsorship, authorization, clearance and
  criminal history, plus residence, location, relocation, commute, on-site, in-office, hybrid, remote, time zone and
  address. The engine sends these fields to neither the short-field model nor the writer; they are answered by
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

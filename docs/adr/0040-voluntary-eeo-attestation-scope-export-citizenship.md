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

## Consequences
More voluntary EEO answers from the owner's stated values, visible blanks instead of silent ones, fewer false
attestation stops, and one more prefilled export-control field. Wording outside the vocabularies stays unresolved
(or declines), so the vocabularies may need generic additions over time. Greenhouse self-identification selects that
exist only in the page DOM are still filled by the browser driver's required-only pass; filling optional DOM-only
blocks is a driver change outside this ADR.

## Verify / revert
`uv run pytest tests/test_qa_round3.py tests/test_gates.py tests/test_mapper_safety.py tests/test_apply_dogfood.py`.
Revert by reverting this PR. The two new `[demographics]` keys have defaults, so existing profiles keep loading.

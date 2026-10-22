# ADR-0038: Mapper safety scoping (privacy-ack order, prior-employee scope, authorization country)

## Context
An independent review found three wrong-answer paths in `apply/mapper.py`:
1. With `acknowledge_privacy_notices = true` (ADR-0034) the privacy-ack check ran before canonical classification, so a
   sponsorship, veteran or Hispanic/Latino question that merely mentioned an "Applicant Privacy Notice" was answered
   "Yes" (or "Yes, I am a protected veteran").
2. `prior_employee` (ADR-0033) answered "No" to any "have you ever worked for ..." question, including U.S. federal
   government, competitor and non-compete questions, for which the profile holds no data.
3. `work_authorized` / `requires_sponsorship` (ADR-0030) ignored the country, so "right to work in the UK" got "Yes"
   from US-only facts.

## Decision
- **Privacy-ack after classification.** `map_fields` computes `canonical_key` first; the ack path runs only when the key
  is `None` or `privacy_ack`. `is_privacy_notice` also rejects any label for which `is_sensitive_question` is true:
  any alias of a `SEMANTIC_SENSITIVE`, demographic, over-18, located-in-US or salary key anywhere in the label
  (alias order ignored), `_DEMOGRAPHIC_LABEL`, or `_SENSITIVE_TOPIC` (sponsorship, visa, citizenship, authorization,
  EEO, veteran, disability, gender, race, salary, employment history ...). A label first matched as `privacy_ack`
  but sensitive is reclassified with `privacy_ack` skipped, so "Gender (see our Privacy Notice)" takes the
  demographic path. The chosen option must itself be an acknowledgement (`is_ack_option`: acknowledge/agree/accept/
  confirm/understand/read, or a bare "Yes"), and is never a sensitive option ("Yes, I am a protected veteran").
- **Prior employee = this company only.** `about_this_company(label, company)` is true only when the label names the
  posting company (alias-tolerant `_co_key`, or its distinctive leading word) or says "this company"/"us"/"here", and
  names no government, agency, military, competitor, non-compete/non-solicit, client/vendor or other employer.
  Otherwise the field is answered only from the user's own `[answers]` entry for that question, else unresolved.
- **Authorization is US-scoped.** `question_countries(label)` detects the US and a list of non-US countries/regions
  (UK, Canada, EU, ...). Structured `[authorization]` facts describe the US and answer only US or unspecified
  questions. A question naming one non-US country is answered only from an `[answers]` pattern that itself names that
  country (`ProfileView.lookup("country_answer:<country>:<question>")`); two or more countries ("US or Canada") stay
  unresolved.

## Consequences
Fewer automatic answers: government/competitor and non-US authorization questions now reach the owner unless an
explicit answer exists. Privacy notices that mention a sensitive topic (for example "protected", "salary") are no
longer auto-acknowledged. The country list is finite; an unlisted country is treated as unspecified.

## Verify / revert
`uv run pytest tests/test_mapper_safety.py tests/test_privacy_ack.py tests/test_apply_mapper_coverage2.py`.
Revert by reverting this PR; no schema or config change is involved.

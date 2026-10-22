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
All three rules are **deny-by-default allow-lists**. A first version used topic/country blocklists; a control review
showed they leaked (H-1B/OPT/TN, VEVRAA, race and religion wording; parent/subsidiary/auditor questions; cities and
unlisted countries), so blocklists are not used to decide that an answer is safe.

- **Privacy-ack.** `map_fields` computes the canonical key first; the opt-in ack path runs only for `None`/`privacy_ack`
  keys. `is_privacy_notice` then requires a privacy phrase and that every word of the label is in `_ACK_VOCAB`
  (privacy/notice/policy, acknowledge/read/understand/agree/confirm, applicant/candidate/data/processing, and
  stopwords). Any other content word ("VEVRAA", "Asian", "religion", "ITAR", "sponsorship") leaves it unresolved.
  The chosen option must match the acknowledgement pattern and also consist only of `_ACK_VOCAB` words.
  Sensitive labels first matched as `privacy_ack` are reclassified (`is_sensitive_question`) so, for example, a gender
  question takes the demographic path; that reclassification is routing only, never a reason to answer.
- **Prior employee.** `about_this_company(label, company, aliases)` replaces the posting company name (alias-tolerant
  `_co_key`, its distinctive leading word, a `dedupe.company_aliases` name for the same employer passed by the engine,
  or "this company"/"us") with a placeholder; the whole label must then match a direct-employment form
  (`_DIRECT_EMPLOY`: "have you (ever) (been employed|worked) by/for/at <company>", "were you employed by <company>",
  "are you a current or former employee of <company>"), with only tense/time words left over. If the company name or
  an alias contains a qualifier word (state, federal, government, agency, competitor, affiliate, parent, subsidiary,
  partner, client, vendor, public, national, department) it is never answered, so "State Street" cannot turn "worked
  for a state agency?" into "No". Everything else uses only the user's own `[answers]` entry, else unresolved.
- **Authorization.** `auth_place_ok` requires every word of the label to be in `_AUTH_VOCAB` (authorization,
  sponsorship and visa wording, visa types, stopwords, and US/U.S./USA/United States/America). Any other word, so any
  city, region, country, "here" or "the country where ...", leaves it unresolved. A label naming exactly one listed
  non-US country (and not the US) is answered only from an `[answers]` pattern naming that country.

## Consequences
Fewer automatic answers. Unusual but harmless wording ("Your authorization to work in the country where you live") now
reaches the owner, and the vocabularies may need occasional additions; each addition must be generic, non-place,
non-topic wording. Wrong answers are replaced by unresolved fields, never the reverse.

## Verify / revert
`uv run pytest tests/test_mapper_safety.py tests/test_privacy_ack.py tests/test_apply_mapper_coverage2.py`.
Revert by reverting this PR; no schema or config change is involved.

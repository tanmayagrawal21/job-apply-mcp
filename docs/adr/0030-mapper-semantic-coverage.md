# ADR-0030: Generic mapper coverage for common screening questions

## Context
Live runs left answerable questions "unresolved" (and so blocked submission): work authorization and sponsorship
phrased in new ways, relocation and hybrid willingness, "do you know anyone at X", years of experience, the year of
the most recent degree, and a programming-language multiselect. The profile held the facts, but `ProfileView.lookup`
only matched the user's own `[answers]` substrings, so any rewording missed.

## Decision
- New canonical keys in `mapper.ALIASES` (`relocation_support`, `open_to_hybrid`, `open_to_onsite`, `knows_employee`,
  `years_experience`, `languages_known`, `us_citizen`) and wider `work_authorized` / `graduation` patterns. All are
  generic label patterns; there is no per-company logic.
- Structured profile facts: `[authorization]`, `[preferences]`, `[skill_years]`, `languages`, `[referrals]`. If a
  fact is unset the code falls back to the user's `[answers]` patterns (several phrasings tried), else stays unresolved.
- Polarity: sponsorship answers are computed from the fact then inverted only when the label is explicitly negated
  ("without sponsorship", "do not require"). A two-part question ("authorized ... and will you require ...") is left
  unresolved. "Will you require relocation support?" is NOT "willing to relocate" and needs its own preference.
- Non yes/no authorization choices ("authorization to work in the country where you live") snap to the single option
  consistent with authorized + sponsorship facts; citizen options need `citizen = true`; ambiguity -> unresolved.
- Referrals: "know anyone / were you referred" -> No unless `[referrals]` lists the posting's company.
- Years: union of the resume's experience spans (profile `[[work]]` as fallback), floored, never rounded up. "At least
  N years" is answered Yes/No by comparison. Per-technology years only from `[skill_years]`; otherwise unresolved.
  Select ranges ("3-5", "10+") use the floor. Limitation: every listed job counts as experience.
- Degree year: latest education end (existing `derive_graduation`); a select without that year stays unresolved.
- Languages: first `apply.top_languages` (default 3) profile languages present among the options; spoken-language
  questions are skipped.
- All new keys are in `SEMANTIC_SENSITIVE`, so the local LLM never guesses them.
- Optional unresolved fields already do not block: `plan()` only reports required fields (or optional textareas when
  `write_optional`); a regression test pins this.

## Consequences
Users must set the new facts once (`examples/profile.example.toml`). Unset facts cost nothing but remain unresolved.

## Verify / revert
`uv run pytest tests/test_apply_mapper_coverage.py`. Revert the PR; profile files with the new keys still load.

## Amendment (ADR-0038)
Authorization and sponsorship answers apply to US or unspecified-country questions only; a non-US country needs an `[answers]` pattern naming it. See ADR-0038.

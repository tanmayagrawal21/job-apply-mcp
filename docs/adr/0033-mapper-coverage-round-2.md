# ADR-0033: Mapper coverage round 2 (identity, location, citizenship, GPA, prior employment, opt-ins, prose routing)

## Context
A live overnight run still stopped on questions the profile can answer: "Full Legal Name", "Your Location" /
"Where are you currently located?", "Citizenship Status", the Greenhouse "authorization to work in the country where you
live" choice (still unresolved after ADR-0030), undergraduate GPA, "previously employed at X", talent-community
opt-ins, "strongest coding language", and several required free-text questions.

## Decision
- Aliases: `full_name` accepts "full legal name"/"your name"; `location` accepts "your location" and "where are you
  (currently) located/based/living"; `languages_known` accepts the singular "strongest coding language".
  "... Name in Native Language" stays unresolved. A text box for "strongest language" gets the profile's top language.
- Authorization options (root cause of the miss): two options survived the filter ("valid work permit and do not need a
  company to sponsor my visa" vs "which needs to be sponsored"), because "do not need" was not read as "no sponsorship"
  and "due to my nationality" was not read as a citizen option. Now a negated-sponsor phrase is excluded when sponsorship
  is required and is the only match when it is not; `nationality` needs `citizen = true`.
- `Authorization.citizenship` (country, optional; falls back to the user's `[answers]` "country of citizenship").
  `us_citizen` is derived from it when `citizen` is unset. "Citizenship Status" selects: citizen False -> the single
  explicit non-citizen option, else the single visa-holder option when authorized; never a citizen, permanent-resident,
  refugee, asylee or DACA option; ambiguity or no match -> unresolved ("Other (please explain)" is never used).
- GPA: the education row whose degree level matches the label (undergraduate = Bachelor, graduate = Master, doctorate);
  an unlabelled "GPA" only when exactly one row has a gpa. Select options: exact value or the single range containing
  it, never rounded; a non-4.0 scale is unresolved.
- Prior employment: "No" unless the profile `[[work]]` or the resume experience lists the posting company (suffix- and
  spacing-insensitive match, so "DoorDash USA" == "DoorDash"). An unknown company is unresolved.
- Marketing/talent-community/newsletter opt-ins: "No". Any label with consent/agree/acknowledge/privacy/GDPR wording is
  left unresolved, as are hybrid-policy commitments and relocation-support questions (owner decisions).
- Prose routing: a free-text question (textarea, or text/unknown with no options and room for prose) whose label
  starts why/what/how/describe/explain/share and is not a name, link, number, location, salary or mapper-known field
  goes to the narrative writer instead of the short-field LLM. Short factual fields never receive LLM text.
  "What AI tools are you using?" is a factual prose question; AI-use attestations and AI-usage selects still need the
  owner. The writer's strict review is unchanged: failed lint or review stays a client_task. With the default
  `writing.reviewer = "client"` every clean draft is itself a client_task for the calling agent; only
  `reviewer = "router"` with a review tier lets a draft pass unattended.
- All new keys are in `SEMANTIC_SENSITIVE`; the local LLM never guesses them.

## Consequences
Users with `[authorization].citizenship` set get citizenship answers; unset facts remain unresolved. Prose questions
asked as single-line text inputs are now written (and reviewed) rather than guessed from a JSON short-answer call.

## Verify / revert
`uv run pytest tests/test_apply_mapper_coverage2.py`. Revert the PR; profiles with `citizenship` still load.

## Amendment (ADR-0036)
`prior_employee` answers only questions about the posting company; government, competitor and other-employer questions need an explicit `[answers]` entry. See ADR-0036.

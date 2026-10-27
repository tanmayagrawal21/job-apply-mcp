# ADR-0064: Candidate journal (`record_candidate_fact`)

Status: accepted · Date: 2026-10-27 · Amends ADR-0023, ADR-0040, ADR-0047, ADR-0050

## Context
Answers the owner gives in chat are lost when the conversation ends. The profile (`profile.toml`) is hand-edited,
and the profile-question store (ADR-0047) takes only values a human types into an elicitation form or the CLI, so a
client never writes it. Every connected AI client (Claude, ChatGPT, an IDE agent) should record what the candidate
says about themselves, wherever it comes up, so later applications can use it. Capture should be cheap and never
refused. Use must stay as safe as the profile's: legal status and demographics cannot come from a model's guess.

## Decision
- **Store.** `<state dir>/candidate_journal.jsonl`: one JSON object per line, file mode 0600, directory 0700,
  stdlib only. It is append-only: a correction is a newer entry for the same key, and `retract_candidate_fact`
  appends a tombstone (`op: "retract"`). No line is ever rewritten or deleted. Each entry carries `id`, `ts`, `date`,
  `kind` (fact | story), `fact`, optional `key`/`value`, `scope` (global | country:XX | company:<name> |
  role:<title>), `source` (chat | correction | document), `quote` (the user's verbatim words), `confidence`
  (stated | inferred | confirmed), `client`, `risk` and `confirmed_via`.
- **Tools.** `record_candidate_fact`, `list_candidate_facts` (newest first, paged, with `usable`) and
  `retract_candidate_fact`. Their descriptions, and a paragraph appended to the server instructions, tell clients
  to call `record_candidate_fact` whenever the user states anything about themselves: residence, authorization,
  visa, availability, salary, preferences, employers, stories, constraints and corrections. The instruction
  paragraph lives in `journal/tools.py` and is appended in its own block at the end of `server.py`, so edits to
  the main instruction text merge without conflict.
- **Capture is never blocked.** Unknown source or confidence values, malformed keys and bad scopes are stored with a
  warning. A malformed key is dropped. A bad scope becomes `invalid:<raw>`, which never matches a posting. The only
  refusals are an empty fact, a key that names a secret (password, code, token, SSN, card), and a fact that is
  nothing but a secret.
- **No secrets.** Before anything is written, fact, quote, value and client pass through `redact_secrets`. It
  replaces passwords ("password is ..."), one-time codes near code words, known token formats, keyword-labelled
  keys, long high-entropy strings, SSNs and Luhn-valid card numbers with `[redacted:<kind>]`. A value that held a
  secret is dropped.
- **Risk classes, gated at use time, not at capture time.**
  - `everyday` facts can fill a form once stated.
  - `sensitive` facts can fill a form only once confirmed. Sensitive means authorization, visa, citizenship,
    sponsorship, immigration, EEO/demographics, criminal history, clearance or background check, decided by the
    key namespace or the text. Text can raise a class but never lower it.
  - Confirmation comes through MCP elicitation at capture time, when the client supports it
    (`confirmed_via = elicitation`). Otherwise, an entry with `source=chat`, a verbatim `quote` and
    `confidence=stated` is accepted as `confirmed_via = client_quote`. A declined dialog, a `document` source
    without a dialog, or a missing quote leaves the entry unconfirmed.
  - `attestation` entries (and any field whose label reads as a certification) are never filled from the journal.
  - `inferred` entries never fill any form field, everyday or sensitive. They are context only.
- **Mapper.** `map_fields(..., journal=JournalView)` wraps the profile in a `ProfileOverlay`, a three-line hook in
  `mapper.py`; the logic is in `journal/overlay.py`.
  - Every lookup asks the profile first. The journal answers only keys the profile leaves empty, through an
    explicit table: `preferences.*`, `contact.city/region/country`, `availability.start_date`,
    `salary.expected`, `employment.current_company/title`, `referral.source`, `authorization.home_country`,
    `authorization.citizenship`, `authorization.<CC>.{work_authorized,requires_sponsorship,
    requires_sponsorship_now,citizen}` and `demographics.<k>`. Other keys are recorded for context and never
    fill a field.
  - Because values go through `lookup`, all of the mapper's existing guards still apply: EEO isolation, per-country
    authorization, residence coupling, option checks.
  - Scope precedence is company > role > country > global, and the newest entry wins within a scope.
  - Each answer that used an entry gets `source = "journal:<id>@<date>"`.
  - When a usable entry and the profile disagree, the field is left unresolved with a `conflict` hint (both
    values, entry id and date) for the owner. Neither value is picked.
- **Stories.** `kind=story` entries never carry a form value. The essay path receives the three most relevant ones
  (word overlap; a company-scoped story only for that company) as `client_tasks[].context.stories` on prose fields.
  `list_candidate_facts(kind="story")` returns them all.

## Consequences
- Facts the owner states in any client survive into later applications without anyone editing `profile.toml`.
- The profile stays authoritative: the journal fills gaps and raises conflicts, and never overrides the profile.
- The journal is client-written, so a client could fabricate a quote. Sensitive facts are therefore accepted only
  with the user's verbatim words (or a dialog the user answered), every journal answer shows its entry and date in
  `src`, and the human approval and plan review steps are unchanged.
- An autonomy-level assumption (ADR-0052) can still fill an unresolved field that is not a fact-rule field; fields
  in conflict that the profile-fact rules own are never assumed.

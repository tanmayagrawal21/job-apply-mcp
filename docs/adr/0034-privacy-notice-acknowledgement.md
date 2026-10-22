# ADR-0034: Opt-in acknowledgement of applicant privacy notices

## Context
Many forms end with "Applicant Privacy Acknowledgement" or "I acknowledge that I have read the privacy policy". Until now
every consent/privacy wording was left unresolved (ADR-0033), so a run stopped on a notice that only informs the
applicant. The owner decided (2026-10-22) that the tool may acknowledge these on their behalf, explicitly and opt-in.

## Decision
- `Preferences.acknowledge_privacy_notices: bool | None = None` in the profile. Only `True` enables the behaviour;
  `None`/`False` leave such questions unresolved exactly as before.
- `mapper.is_privacy_notice(label, stop_phrases)` matches privacy notice / policy / statement / acknowledgement labels
  (`_PRIVACY_LABEL`) and then rejects any label matching `_PRIVACY_EXCLUDE` or containing a `policy.stop_phrases`
  entry (config defaults plus the engine-supplied list). Excluded: authorship ("personally completed", own work), AI use
  and no-AI attestations, truthfulness/accuracy/certification/attestation, background-check, drug-test and screening
  consent, arbitration/waiver, marketing/SMS/newsletter/talent-community opt-ins, terms of employment/use and any label
  that also mentions them. A label that mixes a privacy notice with an excluded topic is therefore unresolved.
- `privacy_ack_value` answers booleans with `True`, and selects/multiselects with the single affirmative option
  (acknowledge/agree/accept/confirm/yes/understand/read, no negator, no decline wording, no excluded wording); zero or
  several candidates, or a text box, stays unresolved. Source is `rule:privacy_ack`.
- `map_fields(..., stop_phrases=)` receives `policy.stop_phrases` from the engine. The check runs before alias lookup,
  so the legacy marketing and consent paths are untouched.
- Docs: generated `docs/configuration.md` gains a profile `[preferences]` section; the example profile (copied by `init`)
  shows the key commented out, i.e. off.

## Consequences
Opted-in users stop being blocked by pure privacy notices. The exclusion list is deliberately broad, so some harmless
labels (for example a notice that mentions "terms of use") still need the owner. Marketing opt-ins stay "No".

## Verify / revert
`uv run pytest tests/test_privacy_ack.py`. Revert by removing the preference, the mapper block and this ADR; with the
preference unset nothing changes, so setting it to `false` is the no-code revert.

## Amendment (ADR-0038)
The check no longer runs before alias lookup: it runs only for fields whose canonical key is `None`/`privacy_ack` and
that are not sensitive questions, and the chosen option must itself be an acknowledgement. See ADR-0038.

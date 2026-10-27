# ADR-0065: Consent-window attestations (`[consent] auto_attest`)

Status: accepted · Date: 2026-10-27 · Builds on ADR-0048 (consent window), ADR-0034/0038 (privacy-notice
acknowledgement, deny-by-default vocabulary), ADR-0050 (messaging opt-ins are owner-only), ADR-0021 (trusted config)

## Context
Required attestation checkboxes ("I certify the information provided is accurate", "I acknowledge employment is
at-will", "I consent to a background check", "I agree to the arbitration agreement / Terms of Service") are on most
forms. Until now each one went to the owner, so a run inside an owner consent window still stopped on them. On
2026-10-27 the owner opted into four classes of these checkboxes, which the tool ticks automatically, but only inside a
consent window that the owner minted.

## Decision
1. **Config.** `[consent] auto_attest = ["accuracy", "policy_ack", "background_check", "arbitration_terms"]`. The
   default is `[]`. The setting is in `_RELAXES`: adding any class counts as a relaxation, so a project file, a
   `JOB_APPLY__CONSENT__AUTO_ATTEST` variable or an untrusted user file is dropped, and only the trusted user config
   may add classes. Those layers may still remove classes. `Settings._auto_attest_trusted` is set by `load_settings`
   only when the trusted user file sets the key, so programmatic Settings fail closed. A trusted opt-in is logged by
   the existing "relaxed by the trusted user config" warning.
2. **Window.** Ticks happen only while `consent.active(settings, "approve_submissions")` returns a valid consent:
   minted by the owner, HMAC-verified, unexpired, unrevoked and covering that scope (for a `--per-run` consent, also
   bound to this run). There is no new crypto and no new scope. The check is read-only. Ticking consumes no
   submission (the submission's own approval does, as before).
3. **Re-check at submit.** On the cached path, the fresh-plan path and the stateless path, `attest.submit_check`
   re-verifies the window, the opt-in and the classification of every auto-ticked field. The stateless path
   reclassifies only when the opt-in is on or an earlier plan of that ref auto-ticked something (server-held
   `attested.json`), so the default configuration is unchanged. If any of these fails, the
   result is `needs_human` and nothing is sent. The stateless path re-derives the ticked fields from the form labels,
   excluding fields that have an explicit owner `[answers]` value.
4. **Classifier (apply/attest.py, deny-by-default).** A field is ticked only if every one of these holds:
   - It is **required** and is not a file. An optional arbitration clause is never agreed to.
   - It is not a messaging opt-in (`mapper.optin_channels`), not a canonical question, not an EEO demographic topic,
     and not a question (`?`). It must contain an attestation verb (certify, acknowledge, understand, agree,
     consent, authorize, read, ...).
   - It matches no term in `_EXCLUDE`. The excluded topics are: AI use, authorship and authenticity; export control,
     ITAR, citizenship and work authorization; criminal history; non-compete and other restrictive covenants;
     certification of a specific fact (degree, license, clearance, years, age, salary, ...); drug, medical and credit
     checks; messaging, marketing and talent-community opt-ins; privacy notices (ADR-0034 keeps its own owner
     preference); data sharing, recordings and signatures; terms or conditions of employment; negations and refusals; first-person statements of fact ("I am",
     "I'm", "I was", "my ... status", "employee").
     A label that combines an allowed class with any of these is excluded.
   - It triggers at least one class, and **every** triggered class is enabled.
   - **Every word** is in the generic vocabulary plus the vocabularies of the triggered classes, after the employer's
     own name (the posting's company only, never `dedupe.company_aliases`, which untrusted config layers can set) is
     removed. Arbitration and terms labels may also name a fixed list of
     ATS vendors. Any other organisation or unknown word leaves the field unresolved. This is the same model as
     `mapper._ACK_VOCAB`.
   - The value is unambiguous: a checkbox gets `True`. A choice field needs exactly one affirmative option made only
     of acknowledgement words, and every other option must be a plain negative ("No", "I do not agree"). Any other option ("Partially",
     "Yes, with exceptions") leaves the field unresolved.
5. **Logging.** Each tick is recorded in four places:
   - As an `Answer` with source `consent:auto_attest:<class>`.
   - In the plan audit `auto_attest`, as `{key, class, label (exact), value, ref, consent_id}`.
   - In the append-only `attested.json` in the ref's evidence folder, once at `plan` and once at `submit`.
   - In the approvals audit log, as an `auto_attest` event with actor `consent:<id>`, written before the click.

   `run_applications` and `run_report` list the ticks under `auto_attested` per posting so the owner can review them.
6. **Placement.** `mapper.py` is unchanged. The hook runs in the engine directly after `map_fields`, so it only ever
   sees fields that every mapper rule left unresolved. Stop-phrase gates (AI-use), the control layer and dedupe still
   run first and are unchanged.

## Consequences
- Outside a window, or with the opt-in missing, behaviour is byte-for-byte the same as before. The tests cover each
  of these cases: no window, an expired window, a revoked window, the wrong scope, an untrusted config, a class that
  is not enabled, and a window that ends between plan and submit.
- Some legitimate wordings will not be ticked: an unlisted word, question phrasing ("Do you consent to a background
  check?"), "contact my references", or a combined privacy notice. They go to the owner, as before. Each new wording
  needs a vocabulary change with a test. A classifier miss is cheap, but a wrong tick is a false legal statement.
- The vocabulary is a frozen allowlist in code, not config, so a config file cannot widen what counts as "common".

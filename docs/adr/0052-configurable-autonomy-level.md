# ADR-0052: Configurable autonomy level for unresolved answers

Status: accepted · Date: 2026-10-23 · Builds on ADR-0040 (never-LLM, EEO isolation), ADR-0047 (profile gaps),
ADR-0050 (contact-permission classifier)

## Context
Owner decision (2026-10-22): when a question has no answer and the user is not available, take the safe answer. The
goal is automation. Questions that keep coming back are a separate story, and a user who wants to play safe can
choose how much the agent may assume.

Until now every unresolved required field stopped the application (`needs_human`). Many of these fields have an answer
that is safe whatever the facts are: declining a contact or marketing permission, declining to self-identify, or the
user's usual "how did you hear about us" source.

## Decision
1. **Setting `answers.autonomy` = `strict` | `safe_default` | `infer`.** The default is `strict`, which keeps the old
   behaviour. Only the trusted user config can make it more permissive. A project file, `JOB_APPLY__*` or an
   untrusted user file can only make it stricter (`config._RELAXES`, by rank strict < safe_default < infer). The
   related keys `promote_after` (it cannot be raised), `infer_min_confidence` (it cannot be lowered) and
   `referral_default` (it cannot be changed) follow the same rule. `job-apply-mcp init` asks for the level
   (`--autonomy` when it runs non-interactively). `control_status` shows it under `autonomy`, and each run and
   run report shows `autonomy`.
2. **strict**: unchanged. Unresolved fields go to the owner.
3. **safe_default**: after the mapper, the LLM tiers and the writer, every field that is still unresolved goes
   through `apply/autonomy.py`, in this order:
   - **Contact and marketing permission asks** get the declining choice: an unticked box (`False`), the single
     No / "No, thanks" / opt-out option, or "None" on a multiselect. ADR-0050 (as narrowed in #73) makes every contact,
     messaging or marketing permission field owner-only and honours only a stored `optin:<channel>`. This ADR owns the
     safe decline. Only a GENUINE permission ask is declined:
     - **The single source of truth** is ADR-0050's `is_single_permission_ask(label, company)`. It accepts one
       anchored permission clause with a named messaging or marketing channel. It rejects a second clause and any
       legal, attestation, EEO or third-party term.
     - **A short supplementary list** (`autonomy._EXTRA_ASKS`) covers common asks that template does not accept.
       Examples are "Can we text you about interview scheduling?", "Join our talent community?" and "Can we email
       or contact you with updates?". Each entry is an anchored whole-label single clause. Each must pass the SAME
       checks: `mapper._SECOND_CLAUSE` and `mapper._SINGLE_ASK_BLOCK`. The only exception is "<verb> or <verb>
       you", a choice of contact verbs inside one clause. The ADR-0050 template itself is never widened.
     - **Anything else is not an ask** and is never declined. It stays unresolved, or at `infer` it may get a
       resume-based answer. Examples: "Would you like to receive WhatsApp training?", "Do you have experience
       writing newsletters?", "Would you like to join our marketing team?", "Would you like to own our SMS
       product?", "SMS opt-in", opt-out wording, and an ask bundled with a privacy policy or arbitration.
     - **Compound labels are never declined, answered or assumed** at any level. Examples: "Have you been arrested,
       and may we text you?" and "May we text you? Are you a US citizen?".
     - Without a clean declining option the field stays unresolved.
   - **"How did you hear about us"** gets `answers.referral_default`, then the `apply.referral_preference` list. On
     a choice field this is the first preference that matches an option (never a referral or employee option), else a
     plain "Other". On free text it is the first preference.
   - **EEO and demographic fields** get the "decline to self-identify" option when the field offers one. The option
     must still pass the ADR-0040 domain check. Otherwise the field stays blank: an optional field is left empty and
     a required one goes to the owner.
   - **Optional free text** stays blank, as before.
   - **Everything else** stays unresolved.
4. **infer**: everything in safe_default, plus skill and experience questions on **required** choice or boolean
   fields. This is an ALLOWLIST (after the control review of #75).
   - The whole normalized label must match one anchored skill template (`autonomy._CAP`): "do you have experience
     with X", "are you proficient / familiar / experienced in X", "are you comfortable using X", "how proficient
     are you with X", "have you used / worked with X", "(N+) years of experience with X", "how many years of
     experience do you have with X", "rate your proficiency with X".
   - X must name a tool, technology, language, domain or skill: at most 4 words, and no person, place, process,
     employment-term or activity word (`_X_DENY`).
   - **Never inferred, so they stay unresolved:** willingness, ability, availability, location, consent and terms
     questions ("are you willing / able / open / comfortable to ...", "comfortable making cold calls?").
   - The field must also clear every exclusion: a hard limit, ADR-0040 legal or place wording, EEO, a blocked
     label, permission wording, or a canonical profile key other than `years_experience`.
   - **#54 gate.** The model call is also gated by the full `never_llm` check and by every `_llm_allowed` reason
     (sensitive keys and labels, attestation or AI use, links and contact values, geo, files). The one exception is
     the "years of" wording of a skill-years question, which the allowlist already anchors.
   - **Provider policy, same as writing.** The call goes through the router (`task_class="short"`), which is
     local-first and honours `llm.allow_cloud`.
     - With `writing.reviewer = "client"` and cloud allowed, infer runs only when every candidate for the short tier
       is local and `client_on_failure` is off. Otherwise the field stays unresolved.
     - `writing.reviewer = "router"`, or `allow_cloud = false`, uses the router as configured.
     - Nothing about the provider is hardcoded.
   - The conservative prompt: Yes only when the facts clearly and directly show it; otherwise the honest No or the
     lowest option; never invent anything; give a short rationale.
   - An affirmative answer is kept only when the confidence is at least `answers.infer_min_confidence` (0.8) AND
     every topic word appears in the resume and profile facts. Otherwise the field gets the lowest value: `False`,
     the single No option, or a single "none / beginner / less than / 0" option.
   - With no model, or no lowest option, the field stays unresolved.
   - The confidence is recorded, and so is the rationale, after it passes through `core.secrets.redact` and is cut
     to 160 characters. The prompt holds profile facts only.
5. **Hard limits at every level** (`autonomy.hard_limit`, checked first): these fields stay structured-facts-only or
   unresolved.
   - authorization, sponsorship, citizenship and visa
   - identity: E-Verify, I-9, proof of identity, passport
   - criminal history, background checks and drug tests
   - screening: polygraph, lie detector, psychological evaluation, physical or medical exams
   - disability: accommodation, essential functions
   - privacy: being recorded, camera or video interviews, social media, monitoring, biometrics, personal data
   - attestations and certifications ("true and complete", signatures)
   - legal agreements: arbitration, NDA, non-compete, confidentiality, terms
   - employment terms: trial period, unpaid or for free, contract or contract-to-hire, 1099/W-2, probation,
     government or military work, firearms, shifts, weekends, nights, overtime, on call
   - salary: any salary field left after the ADR-0047 salary rule, that is, no posted range and no profile policy
   - start date
   - relocation and location: travel, client site, abroad, any named country or region such as "Europe", and the
     ADR-0040 place list

   On a clean contact-permission ask, only the plain "I agree" / "By checking" wording is ignored; every other
   category still applies.
6. **Labelling.**
   - An assumed answer is an `Answer` with `assumed=True`, `source="assumed:<rule>"` (`decline_contact_permission`,
     `referral_default`, `eeo_decline`, `infer`) and a `rationale`.
   - The plan's `compact()` lists them under `assumed`, with the confidence for infer, and `plan.audit["assumed"]`
     keeps the rules.
   - `assumed.json` in the ref's evidence folder records the key, label, value, rule, confidence and rationale.
   - The run result and `run_report` list them per posting under `assumed`.
7. **Pattern tracking.**
   - Each assumption is counted per normalized question kind across runs, in `profile.db` `assumed_hits`. Counting is
     idempotent per application ref.
   - The kinds are `assumed.contact.<channels>`, `assumed.referral_source` and `assumed.q:<normalized label>`.
   - When a kind reaches `answers.promote_after` (default 3), it is recorded as a profile gap with a plain question
     ("... should the answer be Yes or No?"), so `profile_questions` and the start-of-session round ask it once. The
     plan also lists it in `profile_gaps` (`gap`, `assumed_times`).
   - Once the owner stores an answer for the kind (user_written, ADR-0047), it replaces the assumption: it becomes a
     `persona:answer`, no longer assumed. A stored answer that does not fit the field leaves the field unresolved.
   - EEO declines are never promoted, because demographics come from `[demographics]` only.
   - For a contact kind, the owner's own stored answer is the owner deciding. This is the one place, besides
     `optin:<channel>` (ADR-0050), where a Yes can be given.

## Consequences
- At `strict` nothing changes.
- At `safe_default` and `infer` more applications complete without the owner. The worst case of a safe answer is a
  declined contact or marketing permission, a declined self-identification, or a generic referral source.
- At `infer` the worst case is an honest No on a skill the resume does not show, or a Yes the resume clearly supports.
- Fields that could be false or binding statements about the candidate are still never assumed.
- Questions that keep needing an assumption turn into one owner question, then stop being assumed.

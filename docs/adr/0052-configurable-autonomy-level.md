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
4. **infer**: everything in safe_default, plus ordinary capability and experience questions on **required** choice
   or boolean fields:
   - The whole normalized label must match one anchored capability template (`autonomy._CAP`), such as "are you
     comfortable ...", "do you have experience with ..." or "have you used ...".
   - The field must be free of every exclusion: a hard limit, ADR-0040 legal or place wording
     (`never_llm_legal_place`), EEO, a blocked label (ADR-0047 guard), permission wording, a canonical profile key,
     and commitments such as schedules, travel, licences, physical requirements, relatives or AI use.
   - ADR-0050 marks "Are you comfortable making cold calls?" owner-only, so in strict and safe_default it stays with
     the owner. Under infer it is a capability question. The infer path skips only ADR-0050's broad contact clause.
     ADR-0040's anchored legal and place lists still apply in full.
   - One model call (`task_class="short"`) gets a conservative prompt: Yes only when the facts clearly and directly
     show it; otherwise the honest No or the lowest option; never invent anything; give a short rationale.
   - An affirmative answer is kept only when the confidence is at least `answers.infer_min_confidence` (0.8) AND
     every topic word of the question appears in the resume and profile facts. Otherwise the field gets the lowest
     value: `False`, the single No option, or a single "none / beginner / less than" option.
   - With no model, or no lowest option, the field stays unresolved.
   - The confidence and rationale are recorded. The prompt holds profile facts only, never secrets.
5. **Hard limits at every level** (`autonomy.hard_limit`, checked first): authorization, sponsorship, citizenship
   and visa; criminal history, background checks and drug tests; attestations and certifications ("true and
   complete", signatures); legal agreements (arbitration, NDA, non-compete, confidentiality, terms); salary (any
   salary field left after the ADR-0047 salary rule, that is, no posted range and no profile policy); and start date,
   relocation and location facts (including the ADR-0040 place list). These fields stay structured-facts-only or
   unresolved. On a clean contact-permission ask, only the plain "I agree" / "By checking" wording is ignored; every
   other category still applies.
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

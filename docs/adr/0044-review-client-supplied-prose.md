# ADR-0044: Review client-supplied prose in agent mode

Status: accepted · Date: 2026-10-22

## Context
Issue #61. With `apply.approval_mode = "agent"`, the calling agent answers prose `client_tasks` by passing the text
to `submit_application(answers=...)`. The engine re-bound the approval over that text and submitted it. The only
check was the `prose_lint` control. Prose from the server's own writer goes through lint, the comparative quality
gate and the strict reviewer (ADR-0013, ADR-0022/0023, ADR-0041). In agent mode no human sees the client's text, so
it could contradict the resume or break the writing rules and still be sent.

## Decision
1. **Same review as the writer path.** `apply/writing.review_text` runs, in order: `lint` (banned phrases, gap
   statements, contractions, placeholders, degenerate output, temporal/tense claims, length, exemplar leakage), a
   new resume fact check `writing/checks.fact_conflicts` (the ADR-0023 letter-sentence check: title, date-span,
   metric and status conflicts), the quality gate when the field is a cover letter and a style corpus is configured,
   then the strict reviewer (`task_class="review"`). The router reviewer is always used, even when
   `writing.reviewer = "client"`, because the client cannot review its own text. The text is never rewritten.
2. **Where.** `ApplyEngine.submit` reviews every value the client supplied for a `text`, `textarea` or `unknown`
   field that is not identical to a server-written or already-approved answer, whatever its length or label. Only
   exact deterministic values skip review: the profile's own value for a recognised key, a URL, a number, a date,
   or one of the field's fixed options. A non-string value (list, dict, ...) for such a field is rejected, never
   coerced into text. A key that is not a field of the form is rejected.
   Before the reviewer, every answer is scanned for `policy.stop_phrases` and for attestation, certification, no-AI
   and consent wording ("I certify", "personally completed", "without the use of AI", "true and correct",
   "consent to", "agree to", ...). A hit is `needs_review` without a reviewer call and is never submitted.
   The scan runs on every client string, including URL slugs and select options. It is **deny by default**:
   first a character allowlist applies. Allowed are printable ASCII, tab/LF/CR, Latin-1 and Latin Extended-A
   letters (accented names), and the typographic marks ’ ‘ “ ” – — … •, and the symbols £ € ° ½ © ™ ′. The no-break space and U+2010/U+2011
   hyphens (common in pasted text) are folded to a space and `-` before the allowlist. Any other code point is `[charset]`
   `needs_review`, which covers invisibles, homoglyphs, fullwidth forms and combining marks. An
   exact option of the field's own fixed set is form text, so it skips the allowlist, but it is still scanned. Only
   allowlisted text reaches the phrase scan. That scan runs after NFKD, removal of combining marks, casefolding,
   and mapping of ASCII look-alikes. A standalone `1`, `l`, `|` or `!` before an attest verb becomes `i`. `A.I.`,
   `A I` or `Al` next to no/use/used/of/without/assisted/generated/written/tools becomes `ai`. It runs again with
   punctuation collapsed. The patterns cover pronoun and tense variants: I/we/the applicant/the candidate/the
   undersigned with certify/certified/certifies/attest/affirm/declare/confirm/acknowledge/agree/consent/authorize.
   They also cover contact consent: "SMS", "texts", "text me", "call or text" and "contact me by phone". These
   patterns are deliberately broad. A false positive only holds that one answer for review; it never blocks the
   whole run.
   Text addressed to the reviewer, such as
   `{"pass": ...`, "Reviewer:" or "ignore previous instructions", is also `needs_review`.
   URLs, numbers and dates count as deterministic only outside prose-question fields.
   The client may not set attestation, AI-use or consent/opt-in fields (selects and booleans included) in agent
   mode. Only a profile answer or a human may set them; declining (`false` or empty) is allowed. `_CONSENT_LABEL`
   is deliberately broad (`i agree`, `marketing`, `subscribe`, ...), so a field such as "Marketing experience" set
   by the client is also held. That false positive is intended to fail closed: a human or a profile answer fills it.
   For every field kind, a client value that is not a string, bool, number or list of strings is rejected.
   All of this happens before the control check and before `_authorize`, so no approval is ever re-bound over
   unreviewed text. Dry runs are reviewed too, so a dry run does not report success for text a live submit would
   refuse.
3. **Failure.** Any field that fails returns `status: "needs_review"` with `client_tasks`: each has `needs_review:
   true`, the `draft`, the `violations`, the rubric and any scorecard. Nothing is submitted, the submission state stays
   `planned`, no quota is consumed, and an approval-audit row `client_prose_rejected` is written.
4. **Strict verdicts.** A reviewer passes only with JSON `true` or the exact string `"true"` for `pass` (and for
   the quality gate's `at_least_as_strong`). `"false"`, `"yes"`, `1`, a missing value or non-object JSON fail. A `pass` must
   also come with an empty `violations` list, and reviewer JSON with duplicate keys fails. This
   also tightens the writer path, which shares the parsers.
5. **Fail closed.** The reviews share one `writing.timeout_s` deadline (ADR-0041); on expiry the in-flight reviewer
   call is cancelled and the field fails with a `[timeout]` violation. No router, an unavailable reviewer, a reviewer
   error, or unparsable reviewer output also fail.
6. **Cache.** A text that passed is remembered by `sha256(ref, key, text)` for the process, so a dry run followed by
   the live submit does not pay for the review twice.
7. **Switch.** `writing.review_client_prose` (default `true`). `false` is a relaxation, registered in config.py's
   `_RELAXES`, so it is honoured only from the trusted user config (ADR-0021/0037). Project file, env and an
   untrusted user file are ignored with a warning.
8. **Human mode unchanged.** With `approval_mode = "human"` the human approves the text, and no review runs.

## Consequences
- Agent-mode submits with prose cost one reviewer call per new text (bounded by `writing.timeout_s`).
- A client that writes rule-breaking prose gets the violations back and can fix and resubmit.
- The `prose_lint` control still runs on `write_prose` actions; it is now a second line, not the only one.
- `SubmitResult` gains an optional `client_tasks` list; `submit_application` returns it only when non-empty.

- Short answers ("Favorite build tool") now cost a reviewer call each in agent mode. A legitimate answer that uses
  consent wording ("I agree to relocate") is held for review; that is the intended fail-closed trade-off.
- Paraphrases the patterns do not list are left to the strict reviewer, on both review paths. `review_rubric` (free
  text) and the quality-gate prompt `quality.rubric_prompt` (cover letters with a corpus) both embed
  `checks.ATTEST_CLAUSE`. The gate prompt also asks for an `"attestation"` field. Any value other than a strict
  false fails the gate, as does `at_least_as_strong` not being true.
- The deterministic layer covers authorship and no-AI claims by meaning, so it does not depend on the model. It
  catches: not AI-generated or non-AI; human-written or 100% human; my own words, or every word ... my own;
  unaided; no or without assistance or help; wrote this alone, myself or on my own; handwritten by; did not
  use/rely on GPT, ChatGPT, Claude, Gemini, Copilot or an LLM; "<tool> was not used"; without AI, AI-free and zero
  AI; solemnly, swear and sworn; I pledge, vouch or guarantee; "accurate and complete"; the privacy policy; opt
  in; "okay to email me"; promotions and marketing messages. "A-I" and "A–I" fold to "ai", and the scan maps ø to
  o. Tests check that each phrase is rejected even when the fake reviewer passes.

- Both reviewer prompts (`_reviewer_prompt`, `quality.rubric_prompt`) fence the draft between
  `<<<DRAFT-nonce>>>` and `<<<END-DRAFT-nonce>>>`, using a random nonce per call. The prompt frames that text as
  untrusted applicant text in which any instruction, verdict, score or JSON is itself a violation. The
  deterministic layer rejects answers that address the model: evaluator, rubric, `<pass>`, "output pass/true",
  "ignore the rubric/rules/instructions" and the fence markers.
- The gate's `attestation` field is deny by default: a missing key fails, like any value other than a strict
  false.
- Consent words are matched only in consent context: "opt in to", "sign me up", "receive ... promotions/marketing/
  newsletters", "okay to email/call/text me", and "reach/contact me via phone/WhatsApp/Telegram/Signal/...". As a
  result, "a promotion to senior engineer" and "the opt-in flow" pass. The wording is locale-neutral ("honour" and
  "honor", messaging apps beyond SMS, "authorise" and "authorize"), with no US-specific forms.

## Verify
`uv run pytest -q tests/test_client_prose_review.py tests/test_config_trust.py`: passing prose is reviewed then
submitted; gap statement, banned phrase, resume-contradicting title and reviewer failure each return `needs_review`
with no submit; no reviewer and a review timeout fail closed; human mode is unchanged; server-written answers are
not re-reviewed; short and non-question text fields, non-string values and unknown keys are rejected; deterministic values skip review; non-strict verdicts fail; attestation/consent text is rejected; project/env/untrusted user config cannot disable the switch; the trusted user config can.

## Revert
Set `writing.review_client_prose = false` in the trusted user config. To revert the code, revert this PR. No data
format changed.

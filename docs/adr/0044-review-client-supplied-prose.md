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
   The scan runs on every client string, including URL slugs and select options. Any invisible character in an
   answer is itself `needs_review`: the complete Unicode Default_Ignorable_Code_Point set (a range table in
   `writing/checks.py`, including the soft hyphen, CGJ, ZWSP/ZWJ/ZWNJ, bidi controls, variation selectors, Hangul
   fillers, Khmer inherent vowels, Mongolian FVS and tags) plus any other Cf character. Otherwise the text is
   scanned after NFKD, removal of combining marks (Mn/Me), mapping of Cyrillic, Greek and small-capital
   look-alikes to Latin, and NFKC. It is scanned again with punctuation, slug separators and line breaks
   collapsed. Text addressed to the reviewer, such as
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
- Paraphrases the patterns do not list are left to the strict reviewer.

## Verify
`uv run pytest -q tests/test_client_prose_review.py tests/test_config_trust.py`: passing prose is reviewed then
submitted; gap statement, banned phrase, resume-contradicting title and reviewer failure each return `needs_review`
with no submit; no reviewer and a review timeout fail closed; human mode is unchanged; server-written answers are
not re-reviewed; short and non-question text fields, non-string values and unknown keys are rejected; deterministic values skip review; non-strict verdicts fail; attestation/consent text is rejected; project/env/untrusted user config cannot disable the switch; the trusted user config can.

## Revert
Set `writing.review_client_prose = false` in the trusted user config. To revert the code, revert this PR. No data
format changed.

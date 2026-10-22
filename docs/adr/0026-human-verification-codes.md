# ADR-0026: Human-provided verification codes

Status: accepted. Amends ADR-0012 (browser drivers) and ADR-0021 (submission state machine).

## Context

After the single submit click, some sites (Greenhouse "security code", many custom ATSes) email or text a code and
show a prompt. Until now the driver reported `roadblock` and the run ended with the form half-processed and no way
to continue. The code goes to the applicant's own mailbox. This project never reads mail to obtain it: the
applicant is the verifier, and an automated reader would turn "verify you control this address" into a formality
that the tool satisfies by itself.

## Decision

1. **Detection** (`apply/verification.py`, hooked from `PlaywrightDriver._conclude`): after a submit click, the page
   must match a verification text pattern AND expose a visible code input (single field, or a row of
   one-character fields such as Greenhouse `#security-input-0..7`). Several non-split fields are ambiguous and
   become `handoff`. The page is kept open, a pending entry is registered with an expiry
   (`verification.code_ttl_s`, default 600), the submission state becomes `awaiting_code`, and the result is
   `awaiting_code` with a `pending_id`.
2. **Human input only.**
   a. MCP elicitation: `submit_application` asks the user for the code through `ctx.elicit` when the client
      supports it (`verification.elicit`).
   b. Tool `provide_verification_code(pending_id, code)`, documented as "the user types the code they received".
   c. Optional desktop notification (`verification.notify`, default off; fixed text, never the code).
3. **Entry.** Format validation only: characters are letters/digits (spaces and hyphens ignored); length, numeric
   charset and `pattern` come from the page's own input constraints, else `verification.min_length..max_length`.
   A format error costs nothing and nothing is typed. A valid code is typed with real keystrokes
   (`typing_delay_ms`), the page's single verify/submit button is clicked once (more than one candidate means
   `handoff`, never a guess), then the normal confirmation detection runs (network verdict, confirmation URL, text).
4. **One attempt per code** (`verification.max_attempts`, default 1). The attempt is spent before typing. If the
   page rejects the code, the result is `handoff`.
5. **Expiry** moves the prompt to `handoff` with evidence; the tab stays open for the human. Expiry is swept lazily
   (on `provide_verification_code`, on every non-dry `submit`) so no background task outlives the process.
6. **State machine.** `awaiting_code` is blocking: `begin_submit` and `submit` refuse it, so the application is never
   resubmitted. Entering a code moves `awaiting_code -> submitting` atomically (`only_from`), so two callers cannot
   both type. Any unclear outcome (handoff, expiry, roadblock, interruption) is persisted as `unknown` with a
   `handoff:` detail, which is also blocking and resolvable through `resolve_submission`. The tool result still says
   `handoff`.
7. **Never leaked.** The code appears only in function arguments. It is not logged, not in audit rows
   (`code_provided` records that a code was entered, not its value), not in the approvals database, not in
   exception text (driver errors drop their message), not in `SubmitResult`. Code inputs are blanked before any
   screenshot taken after entry. The code does pass through the MCP client that the user chose to type it into;
   that is the user's channel, not ours.
8. **No mail access** is added by this feature, and none may be added to satisfy it. Mail-based confirmation
   dedupe (ADR-0016) is a separate, after-the-fact read and never supplies codes.

## Consequences

- A run no longer ends at the prompt: the human supplies the code in seconds and the flow completes.
- The human remains the verifier; unattended runs simply wait, then hand off.
- Safe-by-default: `enabled=true` only changes behaviour where the site actually shows a code prompt, which was
  previously a dead end. `enabled=false` restores the old `roadblock`.
- `awaiting_code` pages hold a browser tab until completion, expiry, or `aclose`.
- New knobs under `[verification]` (generated in docs/configuration.md).

## Verify

`uv run pytest tests/test_verification.py` (local fixture pages: prompt -> `awaiting_code` -> code -> confirmed,
bad format rejected, wrong code -> `handoff`, expiry -> `handoff`, code absent from logs/evidence/db/audit).
Browser cases need Chrome and skip without it.

## Revert

Set `verification.enabled = false` (behaviour returns to `roadblock`), or revert the PR: the driver hook is two
small edits (`_conclude` and the `finally` guard) plus the `VerificationMixin` base class.

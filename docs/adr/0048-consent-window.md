# ADR-0048: Consent window for unattended runs

Status: accepted · Date: 2026-10-22 · Amends ADR-0021, ADR-0026, ADR-0027, ADR-0036

## Context
The repo owner wants to consent ONCE per period of their choosing (one run, a day, a week). Inside that period no
step should involve a human; the tool applies and the user reviews afterwards. Today each unattended capability
needs its own grant (ADR-0027), `otp_assist` still needs a click per code, and `captcha_policy=attempt` is a
permanent config switch. A batch run (`run_applications`, built separately) needs one question answered: "is this
unattended action covered right now?"

## Decision
1. **One consent, several scopes.** `job-apply-mcp consent --for <dur> | --per-run [--max N] [--scopes ...]`.
   Default scopes: `approve_submissions`, `captcha_attempt`, `otp_unattended`, `credentials` (reserved for the
   credential vault, a separate change; nothing reads it yet). `--status` and `--revoke [id|all]` as for grants.
2. **Minting is human-only.** stdin and stdout must be a TTY, and the human types the exact phrase
   `I consent to unattended applying for <span>` after the scopes, duration, max and OS user are printed. MCP has
   only the read-only `consent_status`; no tool mints, extends or revokes.
3. **Same signed store, separate files.** `ConsentStore` subclasses `GrantStore`: same HMAC secret, hash-chained
   audit, signature/expiry/os_user checks, duplicate-row rejection and max-of-three use counter (signed record,
   audit `use` lines, high-water mark), but its own `consents.json`, `consent_audit.jsonl` and
   `consent_uses.json` / keychain `consent-uses:<id>`. The scope list, mode and per-run binding are in the signed
   payload. `GrantStore` gained small hooks (`record_cls`, file names, `_covers`, `_metered`, `_usable`,
   `_validate`, `_hint`, `_resign`); grant behaviour is unchanged.
4. **Metering.** Only `approve_submissions` consumes a use, so `--max N` = at most N submissions. Other scopes are
   checked and audited as `act` lines, so the code step of the last allowed submission still works.
5. **Durations.** `--for` up to `grants.consent_max_duration_s` (default 7 days). `--per-run` expires after
   `grants.consent_per_run_max_s` (12 h) and binds, on its first use, to the current run: the enclosing
   `consent.run_context(run_id)`, else the server process. Another run is refused. `consent.end_run()` closes it
   early. Both caps are guarded relaxations (`config._RELAXES`): only the trusted user config may raise them.
6. **Interface for other modules.** `consent.active(settings, scope)` (read-only probe: writes nothing, binds
   nothing, consumes nothing) and `consent.use(settings, scope, detail)` (records the action). Unknown scopes raise
   `ValueError`.
7. **approve_submissions.** In `_authorize`, `approval_mode=human` with no human approval yet first tries the
   consent: the record is approved by `consent:<id>` (in the approvals audit log) and the consent audit gets a `use`
   line with the ref. Everything else is unchanged: dry_run, policy gates, stop phrases, caps, dedupe, the
   single-submit state machine.
8. **captcha_attempt.** The ADR-0036 decision moved into `captcha_attempt_ok(policy, widgets, state, consent_ok)`:
   a click happens only when every widget is passive/invisible and nothing is unsafe, AND (`captcha_policy` is
   `attempt` OR a consent covers `captcha_attempt`). For a visible or unsafe challenge the consent is not even
   consulted. ADR-0036's absolute limits are untouched: no solving, no challenge-frame interaction, one click.
9. **otp_unattended.** After the submit click, if the page asks for a code (ADR-0026 detection) and a consent covers
   `otp_unattended`, the engine reads ONLY the newest message from `verification.sender_domains[platform]` received
   within `grants.otp_window_min` minutes and not more than 60 s before the click, through the configured
   read-only mail source (IMAP: `SEARCH FROM <domain>` then `FETCH BODY.PEEK` of the highest sequence number only).
   When that newest message has no code yet (an "application received" mail), it polls every `grants.otp_poll_s`
   for a newer one until `grants.otp_unattended_wait_s` (180 s). Older messages are never consulted. The code is
   format-checked against the page's input, entered once, and the normal confirmation detection runs. No mail in
   time, no mail source, no sender domain, a format mismatch, or a mail error: the prompt is abandoned without
   typing, the state is `unknown` (blocking, never retried) and the result is `roadblock`.
10. **The code never leaks.** It is held in `SecretCode` (redacted str/repr/format, not picklable or
   JSON-encodable) and revealed only as the argument that types it. Audit lines record "code entered (value not
   recorded)". Exceptions during unattended entry are replaced by their type name.
11. **Outside a window nothing changes.** No mail is read, no code is entered; `awaiting_code` goes to the existing
   human/`otp_assist` path, and `approval_mode=human` waits for a human.

## Owner decision and review amendments (round 1)
- **Authority.** The owner decided (2026-10-22, reflected in the reviewer definition) that the tool may enter
  emailed codes and approve submissions unattended ONLY inside an owner-minted consent window, and that no secret
  (code, password, token) may ever reach the LLM, logs or tool output. This ADR implements exactly that gate; it
  does not relax anything outside a window.
- **One code flow per sender domain.** While an `otp_unattended` window is active, submissions to the same mail
  sender (e.g. two postings on one ATS in a parallel batch) are serialized from the click to the code entry, so the
  newest message from that sender can only be this application's code. Outside a window, nothing is serialized.
- **Not English-only.** Code extraction uses cue words in many languages (and CJK without word spacing); without a
  cue it accepts only a single unambiguous 6-8 digit number, else nothing (fail closed). IMAP `SINCE` dates use the
  protocol's fixed month tokens instead of the localized `%b`, which broke searches under non-English locales.
- **LLM isolation.** Tool output for the agent (`submit_application`, `apply_batch`, `consent_status`) carries the
  status and the sender domain only; a test runs a full unattended submission and checks the serialized tool output.

## Consequences
- An unattended run needs one terminal command per period; review happens afterwards (`list_applications`, the
  approvals audit `consent:<id>` actor, `consent_audit.jsonl`).
- This deliberately relaxes ADR-0026's "the human is the verifier" for users who consent: the consent is the
  human's verification decision for that period, with the reads narrowed to one message from one sender.
- A risk engine that rejects without a challenge still looks like "no confirmation" and is not retried.
- Residual risk is the same as ADR-0037: a local process with full file and keychain access as the same OS user can
  roll back all stores together. Consents defend against agents acting through MCP.

## Verify
`uv run pytest tests/test_consent.py tests/test_grants.py tests/test_verification.py` — TTY-only minting and exact
phrase, no minting tool, read-only probes write nothing, expiry, revoke, scopes enforced, tampered scopes/expiry/
binding rejected, grants and consents never mix, `--max` metering and rollback, per-run binding, captcha decision
table, auto-approval logged with the consent id, newest-message-only reads (IMAP fetches one id), mail before the
click ignored, no-mail -> `unknown` and blocking, no window -> no mail read and no code typed, and a seeded property
test that a random code never appears in logs, results, audit rows, files or exception text.

## Revert
Revert the PR. Consents stop being honoured (the files remain and are ignored); grants are unaffected. To disable
without reverting: `job-apply-mcp consent --revoke`.

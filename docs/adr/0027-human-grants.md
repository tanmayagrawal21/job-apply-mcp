# ADR-0027: Human-signed, time-limited grants

## Context
Some steps deliberately need a human (approving a submission in `approval_mode="human"`, typing an emailed verification
code). An owner sometimes wants an agent to do one such step unattended for a bounded time, without weakening the
defaults or giving the agent a way to grant itself rights.

## Decision
1. **Grant record**: `{id, scope, os_user, created, expires, max_uses, nonce}` + HMAC-SHA256 signature, stored in
   `<data_dir>/grants/grants.json`. The per-install secret is in the OS keychain (optional `keyring` extra) or a 0600
   `grant.key` in the user config dir; never in the repo, project file or environment.
2. **Creation is human-only**: `job-apply-mcp grant <scope> --for <dur> [--max-uses N]` refuses unless stdin and stdout
   are TTYs, prints exactly what the scope allows, and requires typing `I authorize <scope> for <dur>`. No `--yes`.
   `--list` and `--revoke [id|all]` are available. No MCP tool creates, extends or revokes; MCP has only `grants_status`.
3. **Enforcement** (`require_grant(settings, scope)` / `try_use`): verifies signature, expiry, os_user and max_uses;
   every use and every refusal is appended to a hash-chained, HMAC-signed audit log (`audit.jsonl`). Use counts come from
   that log; if the chain is broken, limited grants fail closed. Duration cap `grants.max_duration_s` (default 12h) and
   `max_uses_cap`/`otp_window_min` can only be raised from the trusted user config (same rule as ADR-0021 amendment 2).
4. **Scopes**: `approve_submissions` (engine, human mode: a valid grant approves the server-held approval as
   `grant:<id>`, audit-logged; hash binding of answers and the state machine still apply) and `otp_assist`.
5. **otp_assist** (`grants/otp.py`, `assist_code(settings, sender_domain=..., elicit=...)`): needs a grant; reads only the
   single most recent message from the expected sender domain within `grants.otp_window_min` through the configured
   read-only mail source; extracts the code; shows `Code from <sender> at <time>: ••••12 — submit?` in an MCP
   elicitation. Only on human accept does the result carry the code (`status="confirmed"`). Without elicitation support
   it returns `status="fallback"` with the masked code only, for the verification module's human path
   (`provide_verification_code`). The code is never stored, logged or put in an audit line (`OtpResult.code` is
   `repr=False`). The verification module integrates through that one function only.

## Consequences
Residual risk: an agent with shell access as the same OS user can read the file secret (use the keychain) or delete the
audit tail (undetectable truncation) or the grants file (which only reduces rights).
*Amended by ADR-0037:* uses are also kept in a signed `used` counter inside each grant record (re-signed per use), so
truncating the audit tail no longer restores `max_uses`; `grants.use_keyring=false` is honored only from the trusted
user config. Grants defend against an agent
acting through MCP, not against arbitrary local code execution as the owner.

## Verify / revert
`uv run pytest tests/test_grants.py`. Revert the PR; `approval_mode` behavior returns to ADR-0021.

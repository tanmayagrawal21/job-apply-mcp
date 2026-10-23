# ADR-0046: Credential vault, consent-gated ATS logins and account creation

Status: proposed · Date: 2026-10-22 · Amends ADR-0012 (login walls), the "never create accounts" hard stop, ADR-0021/0037 (guarded keys)

## Context
Workday, iCIMS, SuccessFactors and Taleo (and some Greenhouse/Lever flows) require a candidate account before the
form appears. Until now every login wall was a `roadblock`, and account creation was a hard stop. Doing this
unattended needs a credential store whose secrets the calling LLM can never see, and a consent gate a human opens.

## Decision
1. **Vault = OS keychain only** (`keyring` extra). One item per (host, username): service
   `job-apply-mcp-vault:<host>`, account `<username>`. A secret-free index (host, username, added_at) lives in
   `<data_dir>/vault_index.json` (0600) because keychains cannot be enumerated. No file fallback: without a backend,
   or with an insecure one (`fail`, `null`, plaintext, `keyrings.alt`), the vault refuses to store anything. The
   service prefix is a constant, not a setting, so a project file cannot redirect lookups.
2. **Human-only CLI** (TTY required, like `grant`): `job-apply-mcp vault add <host> --username U` (getpass, asked
   twice), `vault list` (hosts and usernames), `vault rm <host> [--username U]`. MCP exposes only `vault_status`
   (hosts with login counts, consent state, pending verifications, this run's flags).
3. **`Secret` type** (`core/secrets.py`). str/repr/format/JSON show `***`; pickling raises; only `reveal()` returns
   the value, and the only production caller is `PlaywrightLoginOps.fill_password` -> `Locator.fill`. Every revealed
   value is remembered in memory so (a) a log-record factory redacts messages and exception text, and (b)
   `core/output.guarded` redacts every MCP tool result and error envelope (raw and JSON-escaped forms). Errors from
   the keychain or Playwright are reduced to their type name and re-raised outside the `except` block, so no chained
   exception carries the value.
4. **Consent gate.** Every credential use and account creation calls `consent.active(settings, "credentials")`
   through the module attribute. `consent.py` here is a stub that returns False for every scope; the consent-window
   PR replaces it. With the stub (or a closed window) the driver behaves exactly as before: a login wall is a
   `roadblock`, and no keychain read happens.
5. **Login walls.** The Playwright driver hands a login page (blocker text, or a visible password field while consent
   is open) to `AccountManager.handle_wall`. Order: host stopped this run -> consent -> visible CAPTCHA (handoff, never
   solved; the same fail-closed `_captcha_state` as ADR-0036) -> vault entry for the **exact** hostname (no parent or
   sibling-tenant fallback; several usernames for one host must match `accounts.signup_email` or the form email,
   else `needs_human`). At most `accounts.max_login_attempts` (default 2, hard max 2) per host per server run. A
   lockout or wrong-password page stops the host for the run and is flagged (`lockout`, `wrong_password`); a 2FA
   code page is `needs_human` (codes are never entered). Flags appear in `apply_batch` as `account_flags` and in
   `vault_status.run_flags`. For Workday/iCIMS/SuccessFactors/Taleo hosts, which have no deterministic filler, the
   driver still signs in so a human continues in the same logged-in profile.
6. **Account creation** only when all hold: no vault entry for the host, consent open, `accounts.auto_create = true`
   (default false; guarded `_bool_on`, so only the trusted user config can turn it on), and not `dry_run`. Once per
   host per run. Required checkboxes are ticked only if they read as terms/privacy and match neither
   `policy.stop_phrases` nor optional-type text (alerts, marketing, talent community); any other required box is
   `needs_human`. Optional boxes stay unticked. The password comes from `secrets` (20-64 chars, default 24; upper,
   lower, digit and symbol; no look-alikes, quotes, backslash, whitespace, triples or a leading symbol). It is stored
   in the keychain **before** any field is filled; the DB (`accounts.db`, table `ats_accounts`) records host,
   username, created_at, status and the terms URL, never the password. A failure before submit or an "account
   already exists" page removes the new keychain item; an unclear result keeps it (`unknown`).
7. **Email verification.** If the site asks to confirm the email, a registered verifier
   (`login.register_email_verifier`) runs only when `consent.active(settings, "mail")`; otherwise, or with no
   verifier (the case today), the account is `pending_verification`.

## Consequences
- The README hard stop "never create accounts" becomes "off by default, trusted config + consent only".
- New guarded keys: `accounts.auto_create` (`_bool_on`), `accounts.max_login_attempts` (`_raises`).
- Residual risk: a secret is in process memory while used, and in the redaction set for the process lifetime. A
  local attacker with the user's keychain access is out of scope. Redaction is defense in depth; the primary rule is
  that no code path returns or logs a `Secret`'s value.
- Selectors for Workday/iCIMS/SuccessFactors/Taleo are best effort and untested against live sites; an
  unrecognised page returns `needs_human`.
- Merge note: the consent-window PR must keep `consent.active(settings, scope) -> bool` and replace `consent.py`.

## Verify
`uv run pytest tests/test_accounts.py` (fake keyring only): vault round trip and exact-host lookup, insecure
backends refused, TTY-only CLI, password policy over 1,000 generations, `auto_create` honoured only from the trusted
user file (project file, env and `JOB_APPLY_MCP_CONFIG` file ignored), nothing used without consent, 2-attempt cap,
lockout/wrong-password stop and flags, CAPTCHA handoff, keychain-before-fill, no password column or bytes in the DB,
terms handling, verification paths, `vault_status` as the only vault tool, and a property test over 60 random
secrets (JSON-special, unicode, format characters) asserting none appears in logs, stdout/stderr, outcomes, run
flags, exceptions, tracebacks or MCP envelopes, even when the page and keychain raise errors that echo the value.

## Revert
Revert the PR. Login walls go back to `roadblock`; stored keychain items stay until removed
(`job-apply-mcp vault rm <host>` before reverting, or the OS keychain app).

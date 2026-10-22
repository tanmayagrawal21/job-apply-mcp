# ADR-0037: Typed trust-boundary guards and signed grant use counters

Status: accepted · Date: 2026-10-22 · Amends ADR-0021, ADR-0027, ADR-0035

## Context
An independent review of main found two holes in the "only the trusted user config may relax a security setting"
rule (ADR-0021):
1. The guards in `config._RELAXES` matched raw values (`v is False`, `isinstance(v, (int, float))`). A *string* in a
   project TOML or env (`[apply] daily_limit = "0"`, `run_limit = "500"`, `grants.max_duration_s = "999999"`,
   `dry_run = "false"`, or env `JOB_APPLY__DRY_RUN=off`) slipped past the guard and pydantic then coerced it to the
   relaxed value.
2. The guards compared against the built-in default, not the user's own value, so a project file or env could raise
   a user's tighter cap (e.g. `daily_limit = 5`) back to the default 25.
Separately, grant uses (ADR-0027) were counted only from audit `use` lines. Deleting or truncating the audit tail
keeps the hash chain valid and silently restored `max_uses`. `grants.use_keyring` was not guarded, so env or a
project file could force the weaker file secret.

## Decision
- **Typed comparison.** Each guarded value from an untrusted layer is coerced with a `pydantic.TypeAdapter` for the
  field's own annotation (the same lax rules the model uses: `"false"`, `"off"`, `"0"`, `"500"`, ...) before the
  relaxation predicate runs. Values pydantic would reject pass through and fail validation as before.
- **User baseline.** Predicates take `(candidate, baseline)`. The baseline is the validated user config (after its
  own restriction when the file is untrusted), which equals the default wherever the user file is silent. A project
  file or env may tighten past it, never loosen it. Legacy `control.params.quota.*` keys use the user's legacy value
  when set, else the user's `apply.*` cap.
- **Caps are whole numbers.** Every submission cap (`apply.*`, legacy `control.params.quota.*`, a rules file's
  `[params.quota]`) must be an int or a string of digits, >= 0 (`config.parse_cap`). `effective_limits` no longer uses
  `int()`, which turned `0.5` into `0` = no limit. An invalid value from ANY source (0.5, "0.5", -1, "1e9", nan),
  including the trusted user file, is replaced by the stricter of the user value and the default and logged. In a
  rules file it is dropped, so the config cap applies. Exactly `0` (off) still comes only from the trusted user config.
- **Defense in depth.** After the merged model is validated, `_enforce` re-checks every guarded field on the typed
  model against the baseline and resets anything looser (logged as an error; should be unreachable).
- **Guarded keys:** `dry_run`, `apply.approval_mode`, `apply.auto_approve`, `apply.daily_limit`,
  `apply.daily_limit_per_platform`, `apply.run_limit`, `control.params.quota.{per_day,per_platform_per_day,per_run}`,
  `control.apply_enabled`, `control.dev_mode`, `grants.max_duration_s`, `grants.max_uses_cap`,
  `grants.otp_window_min`, `grants.use_keyring` (new), `browser.captcha_handoff` (new).
- **Signed use counter.** Each grant record carries `used`, part of the HMAC payload. `check()` bumps it and re-signs
  the record before appending the audit `use` line. The use count comes ONLY from that signed counter, never from the
  audit log, so a truncated or deleted log cannot hand uses back, and a hand-edited counter fails the signature. A
  grant id that appears more than once in `grants.json` is treated as tampering: every copy is rejected and a
  `tampered` audit line is written. Otherwise a copied row would carry its own `used=0`. Records
  without `used` (issued before this change) are refused; grants live at most 12h, so the human re-issues.
- This replaces ADR-0035's "compare to the default, stay stateless" rule.

## Consequences
- A user who relaxed a value in the trusted user file can now have a project/env value *between* default and their
  own value honored (it is tighter than their baseline).
- Env equal to the user's own relaxed value is no longer dropped (it is not a relaxation).
- Residual: an attacker with shell access as the same OS user who restores an earlier snapshot of `grants.json` can
  still restore uses; grants still defend against an agent acting through MCP, not
  arbitrary local code execution (ADR-0027 residual risk, narrowed).
- New guarded settings added later (e.g. a `captcha_policy`) must be added to `_RELAXES` with a typed predicate.

## Verify
`uv run pytest tests/test_config_trust.py tests/test_approvals.py tests/test_daily_limit.py tests/test_grants.py`.
The new file has one string-typed bypass case per guarded key (project file and env), user-tighter-than-default
cases, audit tail/all/delete truncation, a tampered counter, and `use_keyring` via env.

## Revert
Revert the PR. Guards go back to raw-value, default-relative predicates and uses are counted from the audit log only.
Existing grants issued with `used` would then fail signature checks (payload changes) and need re-issuing.

# ADR-0035: Optional, user-facing submission caps

Status: accepted · Date: 2026-10-22 · Amends ADR-0014, ADR-0021

## Context
The `quota` control capped live submits at 25/day, 10/platform/day and 10/run, but only through
`control.params.quota.*`, with hard-coded defaults, no documented way to switch a cap off (TOML has no null)
and no rule about who may raise it. A config the agent can edit or an env var should not be able to remove
the cap.

## Decision
- New settings `apply.daily_limit` (25), `apply.daily_limit_per_platform` (10), `apply.run_limit` (10). `0` means
  no limit for that scope. Negative values are rejected.
- Legacy `control.params.quota.per_day / per_platform_per_day / per_run` keep working and win when the key is set;
  `0` (or an in-code `None`) means off there too. Resolution lives in `control.builtin.effective_limits`.
  Behaviour change: legacy `0` used to block every submit and now means off.
- Loosening a cap (value `0` or above its built-in default) is honored only from the trusted user config
  (ADR-0021 mechanism: `_RELAXES` for `apply.*`, a matching guard for `control.params.quota.*`). Env and project
  config values that loosen are dropped with a warning; lowering is accepted from every source. A value above
  the default from env/project is dropped even if it is below the user's own higher cap (the comparison is to
  the default, so the rule stays stateless).
- `control_status` gains a read-only `daily_limit` block: `limit`, `used` (rolling 24h for day and per platform,
  this run for run) and `remaining` per scope; `null` limit = no limit. `job-apply-mcp doctor` prints the same
  data and warns when a cap is off. Counts use `UsageState`, so `submit_release` (ADR-0032) is honored.
- `init` writes the three keys commented out; `docs/configuration.md` documents both spellings and that `0`
  removes a safety cap.

## Consequences
Dry runs never count, as before. A `rules_file` chosen by project config can still carry `[params.quota]`
(pre-existing gap shared with `[mode]`); not addressed here.

## Verify / revert
`uv run pytest tests/test_daily_limit.py`. Revert: remove `effective_limits`, `daily_limit_status`, the
`apply.*` cap fields and the `CAP_DEFAULTS` guards in config.py.

# ADR-0003: Layered, fully overridable configuration

Status: accepted · Date: 2026-10-22

## Decision
Precedence, lowest to highest: built-in defaults, user TOML (`~/.config/job-apply-mcp/config.toml` or
`$JOB_APPLY_MCP_CONFIG`), project TOML (`./job-apply-mcp.toml`), env vars (`JOB_APPLY__SECTION__KEY`),
then per-call tool arguments. Everything an end user might reasonably tune is a setting: concurrency,
LLM tiers, providers and models, policy gates, stop phrases, enabled platforms, response budget and dry
run. Secrets are read only from the environment or a user-level `.env`, never from project files.

## Verify / revert
`pytest tests/test_core.py -k config_precedence` covers precedence (env coercion and nested-key tests are planned).

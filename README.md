# job-apply-mcp

An MCP server for discovering jobs and applying to them. It is **modular** (pluggable platforms, LLM providers, browser drivers), **parallel** (async fan-out with per-host and per-model limits), **token-frugal** (compact, paginated tool output), and **local-first** (routes LLM work to Ollama / Hugging Face / MLX on your machine and escalates only hard text to a larger model or the calling agent).

> Status: under active construction. Each component lands as its own reviewed PR with a decision record in `docs/adr/`.

## Tools
`search_jobs` · `get_job` · `get_form` · `plan_application` · `submit_application` · `apply_batch` · `list_applications` · `health`

## Design
See `docs/adr/`. In short, adapters exist for Greenhouse, Lever, Ashby, SmartRecruiters and Workday, plus an LLM-heavy `generic` adapter for any other site. The server submits through the platform API when you hold employer credentials, and through a browser driver otherwise. `dry_run` is on by default. CAPTCHAs, verification codes, account creation and no-AI attestations always hand off to a human.

## Quick start
```bash
uv sync --extra dev
uv run job-apply-mcp          # stdio MCP server
claude mcp add job-apply -- uv --directory $(pwd) run job-apply-mcp
```
Configuration: `~/.config/job-apply-mcp/config.toml` (see `examples/config.toml`), overridable with `JOB_APPLY__SECTION__KEY` environment variables.

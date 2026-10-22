# job-apply-mcp

An MCP server that finds jobs and fills applications for you, and stops before anything irreversible. It is
**modular** (pluggable platforms, LLM providers, browser drivers), **parallel**, **token-frugal** (compact,
paginated output) and **local-first** (routine form fields go to Ollama, LM Studio or MLX on your machine; only
hard prose escalates). Works with any MCP client on macOS, Linux and Windows.

## 5-minute quickstart

You need [uv](https://docs.astral.sh/uv/getting-started/installation/) (or pipx). No clone required.

```bash
# 1. Create config.toml, profile.toml, controls.toml (asks a few questions; --yes to skip them)
uvx --from git+https://github.com/tanmayagrawal21/job-apply-mcp job-apply-mcp init

# 2. Put your real details in the profile it printed the path of, then check everything
uvx --from git+https://github.com/tanmayagrawal21/job-apply-mcp job-apply-mcp doctor

# 3. Add the printed snippet to your MCP client (guides for ten clients: docs/clients/)
claude mcp add --transport stdio job-apply -- uvx --from git+https://github.com/tanmayagrawal21/job-apply-mcp job-apply-mcp
```

Then ask your assistant: "search jobs at greenhouse:examplecorp for data roles, plan the best one, and do a dry run."
Everything is a dry run until you set `dry_run = false`.

Install alternatives: `pipx install git+https://github.com/tanmayagrawal21/job-apply-mcp` (then the command is just
`job-apply-mcp`), or `pip install` into a virtualenv. After the first PyPI release, `uvx job-apply-mcp`.
Optional extras: `[browser]` (Playwright), `[agent]`, `[jobspy]`, `[mlx]`, `[hf]`, `[litellm]`, `[keyring]`.

## Features

| Area | What you get |
|---|---|
| Discovery | Parallel search over Greenhouse, Lever, Ashby, SmartRecruiters, Workday and JobSpy boards, paginated |
| Policy gates | Skip by sponsorship, citizenship, clearance, experience, graduation window, salary floor, employer patterns |
| Applying | Platform API where possible, browser driver otherwise, generic LLM-assisted adapter for other sites |
| Local LLM routing | Complexity-scored tiers (small/medium/large/narrative), fallbacks, optional cloud, your MCP client as last resort |
| Writing | Cover letters and answers with style rules, banned phrases and gap-avoidance checks |
| Provenance | Every generated text is recorded and can be verified or edited by you |
| Dedupe | Store of applications plus optional confirmation-email ingest so you never apply twice |
| Control layer | Quotas, mode switch, attestation and recipient guards, prose lint, prompt-injection screen |
| Transports | stdio (default) and streamable HTTP with bearer token |
| Operations | `init`, `doctor`, Dockerfile, CI on Linux/macOS/Windows |

## Safety model

1. **Dry-run by default.** `dry_run = true` unless you change it; `init` always writes it.
2. **Approval tokens.** `plan_application` returns a token bound to a hash of the exact answers. A real submit
   needs that token, so what you reviewed is what is sent (`apply.auto_approve` turns this off; do not).
3. **Control layer.** Independent checks run before every side effect: daily and per-run quotas, single-submit
   guard, recipient deny-list, prose lint, page-text injection screen, `apply_enabled` kill switch. Add your own
   rules in `controls.toml`.
4. **Hard stops.** CAPTCHAs, verification codes, account creation and attestation checkboxes matching
   `policy.stop_phrases` (for example "no AI used") always return `needs_human`. They are not configurable away.
5. **No invented facts.** Answers come from your profile or are marked; unresolved required fields stop the run.
6. **Privacy.** Profile and secrets stay on your machine, are never committed, and API keys are read only from
   the environment. See [SECURITY.md](SECURITY.md).

## Transports

```bash
job-apply-mcp                                                  # stdio (what clients launch)
job-apply-mcp --transport http --host 127.0.0.1 --port 8765   # http://127.0.0.1:8765/mcp
JOB_APPLY_MCP_TOKEN=... job-apply-mcp --transport http --host 0.0.0.0   # non-local: token required
```

HTTP binds loopback by default; any other address refuses to start without `JOB_APPLY_MCP_TOKEN`
(`Authorization: Bearer ...`). Use a TLS reverse proxy for network access. A [Dockerfile](Dockerfile) is included
(no browser by default; `--build-arg BROWSER=1` adds Playwright and Chromium).

## Documentation

- [Client setup guides](docs/clients/README.md): Claude Desktop, Claude Code, Cursor, VS Code, Windsurf, Zed,
  Codex CLI, Gemini CLI, Continue, generic JSON
- [Tool reference](docs/tools.md) and [configuration reference](docs/configuration.md) (generated)
- [FAQ](docs/faq.md), [CHANGELOG](CHANGELOG.md), [design decisions](docs/adr/), [CONTRIBUTING](CONTRIBUTING.md)

## Development

```bash
git clone https://github.com/tanmayagrawal21/job-apply-mcp && cd job-apply-mcp
uv sync --extra dev
uv run ruff check . && uv run pytest -q      # offline; live tests need JAM_LIVE=1
uv run python scripts/gen_config_docs.py     # after changing config.py
```

MIT licensed. Contributions welcome under the [Code of Conduct](CODE_OF_CONDUCT.md).

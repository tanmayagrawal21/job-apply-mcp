# Changelog

Format: [Keep a Changelog](https://keepachangelog.com/en/1.1.0/); versions follow [SemVer](https://semver.org/).

## [Unreleased]

- Human-provided email/SMS verification codes: `awaiting_code` state, `provide_verification_code`, MCP elicitation, optional desktop notification; the tool never reads mail and never stores the code (ADR-0026).
- Human-signed, time-limited grants (`job-apply-mcp grant`), `grants_status`, `otp_assist` (ADR-0027).
- Search sessions, per-host rate limits, progress + `search_status` (ADR-0024).

### Added
- Optional submission caps `apply.daily_limit` / `daily_limit_per_platform` / `run_limit` (`0` = no limit; loosening only from the trusted user config), `daily_limit` block in `control_status` and `doctor` (ADR-0035).
- Resume-first fact precedence, placeholder/addressee/temporal/degeneration/closing lint, comparative quality gate,
  `facts_report` tool, `writing.cover_letter_policy`, tuning degeneration/leakage hard gates (ADR-0023).
- `job-apply-mcp` CLI with subcommands `serve` (default, stdio), `init`, `doctor`, `version`; `python -m job_apply_mcp`;
  console script `job-apply-mcp-init` (ADR-0020).
- Setup wizard: creates `config.toml`, `profile.toml`, `controls.toml` from the fictional examples, detects
  Ollama / LM Studio / mlx_lm.server, picks model sizes for the machine's RAM, always writes `dry_run = true`,
  never overwrites without `--force`, prints the client config snippet.
- `doctor`: config, profile, models, browser and data-directory checks with fixes (`--json`, `--offline`).
- Streamable HTTP transport (`--transport http`), loopback by default, bearer token from `JOB_APPLY_MCP_TOKEN`
  required for any non-loopback bind, unauthenticated `/healthz`.
- Client setup guides for ten clients in `docs/clients/`; `docs/tools.md`, `docs/configuration.md` (generated),
  `docs/faq.md`; SECURITY, CODE_OF_CONDUCT, issue and PR templates; Dockerfile.
- PyPI-ready metadata (classifiers, URLs, SPDX license, `py.typed`); examples are bundled in the wheel.

### Changed
- Default config/data locations follow the platform (Windows `%APPDATA%` / `%LOCALAPPDATA%`; POSIX unchanged).
- All text files are read and written as UTF-8 (fixes Windows code pages).
- `hf_local` device defaults to `auto` (cuda, then mps, then cpu) instead of `mps`.
- CI runs on Ubuntu, macOS and Windows with Python 3.11 to 3.13.

### Security
- Trust-boundary guards compare typed values: string settings such as `daily_limit = "0"` or `dry_run = "false"` in a project file or env no longer bypass them, and env/project can no longer loosen the user config's own tighter value. `grants.use_keyring` and `browser.captcha_handoff` are now guarded (ADR-0037).
- Grant use counts live in the signed grant record, so truncating the audit log no longer restores `max_uses`; grants issued before this change must be re-issued (ADR-0037).

## [0.1.0]
- Initial development release: platform adapters, apply engine, LLM router, control layer, provenance, ingest,
  self-improvement loop, privacy guard.

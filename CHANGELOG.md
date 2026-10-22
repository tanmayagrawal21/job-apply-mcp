# Changelog

Format: [Keep a Changelog](https://keepachangelog.com/en/1.1.0/); versions follow [SemVer](https://semver.org/).

## [Unreleased]

### Added
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

## [0.1.0]
- Initial development release: platform adapters, apply engine, LLM router, control layer, provenance, ingest,
  self-improvement loop, privacy guard.

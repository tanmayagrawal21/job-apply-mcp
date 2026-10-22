# ADR-0004: Supply-chain hygiene

Status: accepted · Date: 2026-10-22

## Context
There are ongoing reports of security vulnerabilities in widely used developer tooling, and some package
and hosting sites are intermittently down. A tool that drives a browser with the user's sessions is a
high-value target.

## Decision
- Minimal core dependencies (`mcp`, `httpx`, `pydantic`). Heavy or optional ones (Playwright,
  transformers, MLX) are extras.
- `uv` with a committed lockfile, and `exclude-newer` set to at least 14 days before the build date, so
  freshly published (possibly malicious) releases are never resolved. Bumping it is a separate,
  reviewable PR.
- CI runs on the lockfile only (`uv sync --locked`).
- No install-time scripts and no auto-update. Model weights come only from the configured local
  runtimes (Ollama / Hugging Face cache).

## Verify / revert
`grep exclude-newer pyproject.toml`; CI fails if the lockfile is out of sync.

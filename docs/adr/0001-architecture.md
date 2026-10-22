# ADR-0001: Architecture: thin MCP tools over pluggable, registry-based components

Status: accepted · Date: 2026-10-22

## Context
We want an MCP server that discovers jobs and applies to them across many platforms. It should not be
tied to one user's workflow, and its components should be buildable, testable and replaceable
independently.

## Decision
- **Layers:** `server.py` (MCP tools, I/O shaping only) → `services.py` (orchestration) → components
  behind Protocols in `core/interfaces.py`: `PlatformAdapter`, `LLMProvider`, `BrowserDriver`, `Store`.
- **Registries** (`core/registry.py`): components self-register by name, and config selects which are
  enabled. Adding a platform or provider is a new file plus a test, with no edits to the server.
- **Platforms:** dedicated adapters for popular ATSs with public APIs (Greenhouse, Lever, Ashby,
  SmartRecruiters, Workday CXS). A `generic` adapter is LLM-heavy: it reads any HTML form and maps
  fields with a model.
- **Submission path preference:** platform API, when the operator holds employer credentials (for
  example a Greenhouse Job Board API key), then browser driver, then hand-off to a human. Candidate-side
  API submission without employer keys is not offered by Greenhouse, Lever or Ashby, so the browser
  driver is the common path.
- **Safety defaults:** `dry_run = true`. Hard stops on CAPTCHA, emailed codes, account creation, and
  attestations that the applicant personally wrote the application or used no AI (configurable
  `stop_phrases`). These outcomes become `needs_human`.

## Consequences
- Each component PR can be verified against its Protocol in isolation and reverted alone.
- The generic adapter costs more tokens. The LLM router (ADR-0006) keeps that cost local where possible.

## Verify / revert
Verify: `pytest tests/test_core.py` (registry; per-interface conformance tests land with each component PR). Revert: `git revert` the squash commit for this ADR's PR.

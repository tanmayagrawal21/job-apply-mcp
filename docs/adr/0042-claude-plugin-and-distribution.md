# ADR-0042: Claude Code plugin "ApplyWise" and public distribution

Status: proposed (draft PR; release waits for the open fixes and a clean QA round) · Date: 2026-10-22

## Context
The owner wants the server installable as a Claude Code plugin named ApplyWise (id `applywise`) and listed in
public MCP directories (PyPI, the official MCP Registry, Smithery, mcp.so, Glama, PulseMCP). Installing a
plugin makes every user's session run whatever command `.mcp.json` names, so the packaging must not widen what
the server can do, and must not run unreviewed code.

## Decision
1. **Plugin in the repo root.** `.claude-plugin/plugin.json` (`name: applywise`, `version` = package version,
   author = GitHub handle only), `.claude-plugin/marketplace.json` (marketplace `applywise`, one entry with
   `source: "./"`, no entry `version` so `plugin.json` is the single source), `.mcp.json` with one stdio server
   `applywise`, and one skill `skills/applywise/SKILL.md`. Install:
   `claude plugin marketplace add <owner>/job-apply-mcp` then `claude plugin install applywise@applywise`
   (exact commands in the README).
2. **Pinned server.** `.mcp.json` runs `uvx --from git+<repo URL>@<40-char SHA> job-apply-mcp`
   now, and `uvx applywise@<version>` once PyPI has the release. A branch or unversioned spec is
   rejected by a test. Bump procedure: `docs/release.md`.
3. **Generic skill only.** The skill teaches the safe flow (init/doctor, search → plan → dry run → review →
   approve, human vs agent approval, CAPTCHA handoff, `captcha_policy=attempt` as user-config-only opt-in,
   grants, daily limits, never passwords or CAPTCHAs, never run the human-only CLI commands). It contains no
   personal data and no machine paths. The maintainer's private review/privacy/control/QA agents are not shipped.
4. **Safe defaults are tested.** With no user config: `dry_run=true`, `approval_mode=human`, `auto_approve=false`,
   `captcha_policy=handoff`, `writing.reviewer=client`, and daily/platform/run caps 25/10/10 (all non-zero), both
   for `Settings()` and `load_settings()`.
5. **PyPI distribution renamed to `applywise`.** Console scripts `applywise` and `job-apply-mcp` (alias) both call
   `job_apply_mcp.cli:main`; `job-apply-mcp-init` is kept. The import package stays `job_apply_mcp`, and config/data
   directories keep the `job-apply-mcp` name, so existing installs keep their files. `__version__` reads the
   `applywise` metadata, falling back to `job-apply-mcp` for pre-rename installs. Classifier changed to
   `3 - Alpha` to match the README warning. License: MIT (PEP 639 `license = "MIT"`).
6. **MCP Registry.** `server.json` (`io.github.<owner>/applywise`, pypi package `applywise`, stdio,
   `runtimeHint: uvx`); ownership proof is the `mcp-name:` HTML comment at the top of the README,
   which is the PyPI long description.
7. **MCPB bundle for Smithery.** MCPB manifest 0.4 has a `uv` server type that runs a Python entry point from a
   `pyproject.toml` without bundling dependencies, so `mcpb/` holds `manifest.json` (type `uv`), a pyproject that
   depends on `applywise==X.Y.Z`, and a three-line `src/server.py`. It can only be packed after the PyPI release.
8. **Release workflow (not added in this PR).** Intended `.github/workflows/release.yml`: on tag `v*`; top-level
   `permissions: {}`; job `build` (`contents: read`: check the tag equals every pinned version, `uv sync --locked`,
   ruff, pytest, `uv build`, smoke-test both scripts, upload `dist/`); job `pypi` (environment `pypi`,
   `id-token: write` only, `pypa/gh-action-pypi-publish`, trusted publishing, no tokens); job `mcp-registry`
   (`id-token: write`, `contents: read`; download a pinned `mcp-publisher` release and verify its sha256, wait for
   PyPI to serve the version, `mcp-publisher login github-oidc`, `publish`). All actions pinned by commit SHA.
   Writing a workflow that publishes publicly was blocked by the agent's permission policy pending the owner's
   explicit approval, so it is described here and in the PR for the owner to add or approve.

## Consequences
- A root `.mcp.json` is also a project-scope MCP config: contributors who open this repo in Claude Code are asked
  whether to enable the `applywise` server. Declining is harmless.
- Every release now touches five version fields; `tests/test_plugin_packaging.py` and the tag check keep them in
  step. Because `plugin.json` sets `version`, plugin users get nothing new until it is bumped.
- `applywise --help` still shows `prog` as `job-apply-mcp` (cosmetic; `cli.py` is outside this PR's scope).
- The distribution rename changes `uv.lock`'s project entry only.
- The owner's GitHub handle is on the private-terms list, but manifests, the registry name and the install command
  require it. `.privacy-allow` gets one fully anchored regex per required line (exact whole-line match), so no
  other text on those lines, and no other file, is exempted. Prose docs and tests use `<owner>` or derive it from
  `pyproject.toml`.

## Verify
`uv run pytest -q tests/test_plugin_packaging.py`; `claude plugin validate --strict .`;
`claude --plugin-dir .` then `/mcp` shows `plugin:applywise:applywise` and `/applywise:applywise` loads the skill.

## Revert
Delete `.claude-plugin/`, `.mcp.json`, `skills/applywise/`, `server.json`, `mcpb/`, `docs/release.md`, the test
and this ADR; restore `name = "job-apply-mcp"` in `pyproject.toml`, `__init__.py` and the README section; `uv lock`.

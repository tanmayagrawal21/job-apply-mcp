# ADR-0042: Claude Code plugin "ApplyWise" and public distribution

Status: accepted (owner authorized publishing once merged) · Date: 2026-10-22, amended 2026-10-23

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
   approve, runs from links, self-emailed links and discovery with `max_applications`, run statuses, the
   profile builder, per-country authorization, owner-only contact permissions, human vs agent approval, the
   consent window and unattended codes, vault logins the model never sees, CAPTCHA handoff, grants, daily
   limits, never run the human-only CLI commands). It contains no
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
8. **Release workflow** `.github/workflows/release.yml`, on tag `v*`, top-level `permissions: {}`, every action
   pinned by commit SHA:
   - `build` (`contents: read`): the tag must equal every pinned version (pyproject, plugin.json, server.json x2,
     mcpb manifest and pin); `uv sync --locked`, ruff, pytest, `uv build`, smoke-test both scripts from the wheel.
   - `pypi` (environment `pypi`, `id-token: write` only): `pypa/gh-action-pypi-publish` with Trusted Publishing.
     No API token exists anywhere.
   - `mcp-registry` (`id-token: write`, `contents: read`): a pinned `mcp-publisher` release, sha256-verified; waits
     until PyPI serves the version; `mcp-publisher login github-oidc` then `publish`.
   - `github-release` (`contents: write`): packs `mcpb/` with a pinned `@anthropic-ai/mcpb` and creates the GitHub
     Release for the tag with the `.mcpb` bundle and the sdist and wheel.
9. **Committed allowlist for the public identity (reconciled with ADR-0051).** The scanner has two separate
   mechanisms. The committed `.privacy-allow` (regexes, read from the repo root in every mode, diff included)
   is the long-standing exemption for fictional fixtures. ADR-0051's `--allow` file holds salted fingerprints of
   private terms, so it must stay outside every repo and only applies to whole-tree scans; its in-repo refusal
   is about that file, not `.privacy-allow`. CI's diff scan runs without a terms file, and a bare handle matches
   no built-in detector (email, phone, address, home path, secret), so CI never flags the manifests. The owner's
   local scans with private terms do, and `@urls` covers only `https://github.com/<owner>` URLs, not
   `"name": "<owner>"`, the registry name `io.github.<owner>/applywise` or `marketplace add <owner>/...`. So
   `.privacy-allow` keeps one regex per required line, anchored at both ends, holding only public identity strings
   (the handle and the repo URL); no private term, fingerprint or salt is ever added there.

## Consequences
- A root `.mcp.json` is also a project-scope MCP config: contributors who open this repo in Claude Code are asked
  whether to enable the `applywise` server. Declining is harmless.
- Every release now touches five version fields; `tests/test_plugin_packaging.py` and the tag check keep them in
  step. Because `plugin.json` sets `version`, plugin users get nothing new until it is bumped.
- `applywise --help` still shows `prog` as `job-apply-mcp` (cosmetic; `cli.py` is outside this PR's scope).
- The distribution rename changes `uv.lock`'s project entry only.
- The owner's GitHub handle is on the private-terms list, but manifests, the registry name and the install command
  require it. `.privacy-allow` gets one fully anchored regex per required line (exact whole-line match), so no
  other text on those lines, and no other file, is exempted (item 9). Prose docs and tests use `<owner>` or derive
  it from `pyproject.toml`.
- Publishing needs one-time owner setup (PyPI trusted publisher, `pypi` environment); see `docs/release.md`.

## Verify
`uv run pytest -q tests/test_plugin_packaging.py`; `claude plugin validate --strict .`;
`claude --plugin-dir .` then `/mcp` shows `plugin:applywise:applywise` and `/applywise:applywise` loads the skill.

## Revert
Delete `.claude-plugin/`, `.mcp.json`, `skills/applywise/`, `server.json`, `mcpb/`, `docs/release.md`,
`.github/workflows/release.yml`, the identity lines in `.privacy-allow`, the test
and this ADR; restore `name = "job-apply-mcp"` in `pyproject.toml`, `__init__.py` and the README section; `uv lock`.

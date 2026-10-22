# Cutting a release

ApplyWise ships through four channels from this one repository (ADR-0042):

| Channel | File(s) | Version source |
|---|---|---|
| PyPI distribution `applywise` | `pyproject.toml` | `[project].version` |
| Claude Code plugin `applywise@applywise` | `.claude-plugin/plugin.json`, `.claude-plugin/marketplace.json`, `.mcp.json`, `skills/` | `plugin.json` `version` plus the pin in `.mcp.json` |
| Official MCP Registry (name in `server.json`) | `server.json` | `version` and `packages[0].version` |
| MCPB bundle (Smithery) | `mcpb/` | `manifest.json` `version` and the `applywise==` pin in `mcpb/pyproject.toml` |

`tests/test_plugin_packaging.py` fails if these disagree, if `.mcp.json` is not pinned, or if the safe defaults
change.

## Why `.mcp.json` is pinned

Claude Code installs the plugin from the default branch, and every session runs whatever `.mcp.json` names. An
unpinned `git+...` URL would run the newest commit on `main` on every user's machine, unreviewed. So the server
is always pinned:

- **Before the first PyPI release (now):** a full 40-character commit SHA or a release tag:
  `uvx --from git+<repo URL>@<sha-or-vX.Y.Z> job-apply-mcp` (repo URL as in `pyproject.toml`)
- **After the first PyPI release:** an exact PyPI version: `uvx applywise@<version>`. The git form above stays the
  documented fallback (for example if PyPI is unreachable or a fix is needed before a release).

Never use a branch name (`@main`) or an unversioned `uvx applywise`.

## Bumping the pin (every release)

1. Pick the version `X.Y.Z` and set it in all of: `pyproject.toml`, `.claude-plugin/plugin.json`,
   `server.json` (`version` and `packages[0].version`), `mcpb/manifest.json`, and `applywise==X.Y.Z` in
   `mcpb/pyproject.toml`. Run `uv lock`.
2. Add a `CHANGELOG.md` entry.
3. Check: `uv run ruff check . && uv run pytest -q` and `claude plugin validate --strict .`
4. Merge to `main` through a normal PR, then tag that merge commit: `git tag vX.Y.Z && git push origin vX.Y.Z`.
5. After the package is on PyPI, open a follow-up PR that sets `.mcp.json` to `"args": ["applywise@<version>"]` and
   updates the two `init`/`doctor` commands in `skills/applywise/SKILL.md` to `uvx applywise@<version> ...` (the
   test checks they match). Until PyPI is live, pin `.mcp.json` to the tag (`@vX.Y.Z`) or the tagged commit's SHA.
6. Users get it with `claude plugin update applywise@applywise`. Because `plugin.json` sets `version`, users do
   not receive changes until the version is bumped.

## Release workflow (pending)

The tag-triggered workflow `.github/workflows/release.yml` (build with uv, run tests, publish to PyPI with
trusted publishing, then publish `server.json` to the MCP Registry with `mcp-publisher login github-oidc`) is
**not in this repository yet**: adding a workflow that publishes publicly needs the owner's explicit sign-off.
Its intended shape is in ADR-0042. Until it lands, steps (c) and the registry publish below are manual.

## Owner actions

These need the repository owner's own accounts and cannot be done by an agent.

- **(a) PyPI trusted publisher.** Create a PyPI account (enable 2FA). Under *Your account → Publishing → Add a
  pending publisher*, add: project `applywise`, owner = the GitHub account that owns this repository, repository `job-apply-mcp`, workflow
  `release.yml`, environment `pypi`. No API token is created or stored.
- **(b) GitHub environment.** In the repository, *Settings → Environments → New environment* named `pypi`.
  Recommended: required reviewer = you, and deployment tags limited to `v*`.
- **(c) Tag.** After the release PR merges: `git tag v0.x.y && git push origin v0.x.y`. The release workflow (once
  added) publishes to PyPI, then to the MCP Registry. Manual fallback for the registry:
  `mcp-publisher login github && mcp-publisher publish` from the repo root.
- **(d) mcp.so.** Submit the repository URL at https://mcp.so/submit.
- **(e) Glama.** Claim the listing for the repository on glama.ai (Glama indexes public MCP repositories; claiming
  lets you edit it).
- **(f) Smithery.** Build the bundle once `applywise==X.Y.Z` is on PyPI:
  `npx @anthropic-ai/mcpb pack mcpb applywise.mcpb`, then publish it with
  `smithery mcp publish ./applywise.mcpb -n <owner>/applywise` (or upload it at smithery.ai/new).
- **(g) PulseMCP.** Nothing to do: it picks up the listing from the official MCP Registry automatically.

## Before the first release

The owner wants the first release only after the open fixes (#53 and the QA mapper/latency PRs) land and a QA
round is clean. Keep the plugin PR in draft until then.

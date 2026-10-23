# Releasing ApplyWise

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

## Cutting a release

1. Pick `X.Y.Z` and set it in all of: `pyproject.toml`, `.claude-plugin/plugin.json`, `server.json` (`version`
   and `packages[0].version`), `mcpb/manifest.json`, and `applywise==X.Y.Z` in `mcpb/pyproject.toml`. Run
   `uv lock`. Add a `CHANGELOG.md` entry.
2. Check: `uv run ruff check . && uv run pytest -q` and `claude plugin validate --strict .`
3. Merge to `main` through a normal PR (reviewer, privacy agent, fresh CI).
4. Tag the merge commit and push the tag: `git tag vX.Y.Z <merge-sha> && git push origin vX.Y.Z`.
5. `.github/workflows/release.yml` runs on the tag:
   - **build**: fails unless the tag equals every version above; `uv sync --locked`, ruff, pytest, `uv build`,
     smoke-tests `applywise version` and `job-apply-mcp version` from the wheel.
   - **pypi**: publishes the sdist and wheel with Trusted Publishing (OIDC, environment `pypi`). No token.
   - **mcp-registry** (environment `pypi`): waits until PyPI serves `applywise==X.Y.Z` (the registry checks the `mcp-name:` line in
     the PyPI description), then `mcp-publisher login github-oidc` and `mcp-publisher publish` for `server.json`.
   - **mcpb** (read-only): `npm ci --ignore-scripts` from `.github/release-tools/package-lock.json`, then packs
     `mcpb/` into `applywise-X.Y.Z.mcpb` and uploads it as an artifact.
   - **github-release** (the only job that can write): downloads the artifacts and creates the GitHub Release
     with the `.mcpb`, sdist and wheel. To bump mcpb, edit `.github/release-tools/package.json` and run
     `npm install --package-lock-only --ignore-scripts` there.
6. Follow-up PR: switch `.mcp.json` to `"args": ["applywise@<version>"]` and the two `init`/`doctor` commands in
   `skills/applywise/SKILL.md` to `uvx applywise@<version> ...` (the test checks they match).

A failed job can be re-run from the Actions tab. PyPI never accepts the same version twice, so a broken release
is fixed with a new patch version, not by moving the tag.

## Bumping the pin

Until PyPI has the release, `.mcp.json` and the skill pin a full commit SHA on `main` (or the release tag).
After it, they pin `applywise@<version>`. Users get a new version with `claude plugin update applywise@applywise`;
because `plugin.json` sets `version`, nothing reaches them until it is bumped.

## One-time owner steps

These need the repository owner's own accounts and cannot be done by an agent. Do (a) and (b) before the first
tag; skip any already done.

- **(a) PyPI trusted publisher.** Create a PyPI account (enable 2FA). Under *Your account → Publishing → Add a
  pending publisher*, add: project `applywise`, owner = the GitHub account that owns this repository, repository `job-apply-mcp`, workflow
  `release.yml`, environment `pypi`. No API token is created or stored. (Once the project exists, the same
  entry lives under the project's *Settings → Publishing*.)
- **(b) GitHub environment.** In the repository, *Settings → Environments → New environment* named `pypi`.
  Recommended: required reviewer = you, and deployment tags limited to `v*`. Both the PyPI and the MCP Registry
  jobs use it.
- **(c) MCP Registry.** Nothing to set up: GitHub OIDC from this repository proves the `io.github.<owner>`
  namespace. Manual fallback: `mcp-publisher login github && mcp-publisher publish` from the repo root.
- **(d) mcp.so.** Submit the repository URL at https://mcp.so/submit.
- **(e) Glama.** Claim the listing for the repository on glama.ai (Glama indexes public MCP repositories; claiming
  lets you edit it).
- **(f) Smithery.** Download `applywise-X.Y.Z.mcpb` from the GitHub Release and publish it with
  `smithery mcp publish ./applywise-X.Y.Z.mcpb -n <owner>/applywise` (or upload it at smithery.ai/new).
- **(g) PulseMCP.** Nothing to do: it picks up the listing from the official MCP Registry automatically.

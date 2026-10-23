# ADR-0054: One path resolver honouring XDG_*; serverInfo reports the package version

Status: accepted · Date: 2026-10-23 · Issues: #82, #83

## Context
A clean-install smoke test with XDG_CONFIG_HOME / XDG_DATA_HOME / XDG_CACHE_HOME set to temp dirs saw the server
read the default `~/.config` config while `doctor` reported the XDG path. Separately, MCP `serverInfo.version` was
the `mcp` library's version (1.30.0), not ours.

## Decision
1. `job_apply_mcp.paths` is the only place that resolves user-level locations: config, data, cache, profile,
   controls, privacy terms/allow files, and (under data) consent, approvals and vault metadata. Scripts import it
   instead of hardcoding `~/.config`.
2. `XDG_*_HOME` is honoured on every OS when set to an absolute path. Relative or empty values are ignored (XDG
   spec): a relative value resolves against each process's cwd, so a server launched by a client and `doctor` run
   from a shell could read different files. Fallbacks: `~/.config`, `~/.local/share`, `~/.cache`
   (Windows: `%APPDATA%`, `%LOCALAPPDATA%`, `%LOCALAPPDATA%\job-apply-mcp\Cache`). The HTTP cache stays under
   `<data_dir>/http_cache` (configurable); `cache_dir()` is the resolver for future cache users.
3. Trust model unchanged: the trusted user config is the file at `paths.default_config_file()` (the resolved
   `<config dir>/config.toml`), owned by the user and not group/world-writable. `JOB_APPLY_MCP_CONFIG` files and
   project files are never trusted to relax settings (ADR-0021).
4. `serverInfo.version` is `job_apply_mcp.__version__`.

## Note
Clients that spawn the server with a scrubbed environment (the MCP SDK passes only HOME, PATH and a few others
unless `env` is given) drop `XDG_*`; the server then correctly falls back to `~/.config`. Pass the variables in
the client's `env` block.

## Amendment: fixed trust and state locations (control review of #84)
The trusted config and the security-state stores must not follow an environment that a project can set (a repo's
`.mcp.json` `env` block can set `XDG_*`).
1. The TRUSTED user config is `<account home>/.config/job-apply-mcp/config.toml` (POSIX; home from the user
   database via `pwd`, not `$HOME`) or `<profile>\AppData\Roaming\job-apply-mcp\config.toml` (Windows). A config found through
   `XDG_CONFIG_HOME` or `JOB_APPLY_MCP_CONFIG` still loads (so a smoke test can isolate), but as UNTRUSTED: it can
   only make settings stricter. The owner and group/world-writable checks are unchanged.
2. Consent, grants, approvals, the control audit/quota log, the grant key file and use high-water marks live under
   `paths.state_dir()` (`<account home>/.local/share/job-apply-mcp`, Windows `<profile>\AppData\Local`), ignoring
   `XDG_DATA_HOME`, unless the TRUSTED user config sets `paths.state_dir` (any other source of it is dropped).
   Ordinary data (evidence, search cache, applications db) and the cache keep following XDG.
3. Revocation is bound to the keychain: `revoke` records the ids as tombstones next to the signing key (keychain
   entry `revoked:<prefix><id>`, plus a file in the fixed config dir). A grant or consent with a tombstone fails
   verification, so restoring an old copy of the data dir cannot revive it.
4. Hooks: `scripts/install-hooks.sh` warns when the terms path cannot be resolved or no terms file exists, and
   blocks the commit when a terms file exists but cannot be loaded.

## Amendment: migration and the dedupe store
- `applications.db` (dedupe store and intake tables) joins the fixed state dir: a project file or env that sets
  `paths.data_dir` can no longer hide already-submitted postings.
- `job_apply_mcp.migrate.ensure_state` runs the first time the state dir is resolved. It discovers old locations
  read-only and never trusts them: the effective `paths.data_dir`, `paths.data_dir` named in any config file (fixed,
  XDG, `JOB_APPLY_MCP_CONFIG`, project), the XDG-honouring data dir, the default dir, and the old XDG config dir
  (for `grant.key` and high-water files). Missing state is copied, never moved or deleted; existing state is UNIONED.
  Most restrictive wins: `confirmed > submitted > unknown > submitting > ... > planned` per ref; `used`/`denied`
  approvals beat `pending`/`approved`; a posting that really went out wins in `applications`; the quota log is
  merged by line (replay is by event id); consents/grants take the earliest expiry and highest use count (re-signed
  with the install key); revoked ids are never revived.
- Any failure raises `StateMigrationError`: the stores refuse to open (no submit) and `doctor` reports `state_dir`
  FAIL with the reason. A marker file records merged sources so reruns are cheap.

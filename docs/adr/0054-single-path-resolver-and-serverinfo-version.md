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

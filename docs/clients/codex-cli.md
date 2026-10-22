# Codex CLI

Vendor docs: https://learn.chatgpt.com/docs/extend/mcp?surface=cli (redirected from
developers.openai.com/codex/mcp)  (Verified against the vendor documentation on 2026-10-22.)

Config: `~/.codex/config.toml` (or `.codex/config.toml` in a trusted project). Note `mcp_servers` with an
underscore, TOML not JSON.

```
codex mcp add job-apply -- uvx --from git+https://github.com/tanmayagrawal21/job-apply-mcp job-apply-mcp
```

or by hand:

```toml
[mcp_servers.job-apply]
command = "uvx"
args = ["--from", "git+https://github.com/tanmayagrawal21/job-apply-mcp", "job-apply-mcp"]
startup_timeout_sec = 30   # first run downloads dependencies
```

HTTP with a bearer token read from an environment variable:

```toml
[mcp_servers.job-apply-http]
url = "http://127.0.0.1:8765/mcp"
bearer_token_env_var = "JOB_APPLY_MCP_TOKEN"
```

List with `codex mcp list`.

**Not verified:** whether `auth` must be set to disable the default OAuth attempt for a plain bearer-token
server; set `bearer_token_env_var` and test.

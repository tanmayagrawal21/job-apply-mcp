# Claude Code

Vendor docs: https://code.claude.com/docs/en/mcp  (Verified against the vendor documentation on 2026-10-22.)

stdio (recommended). Everything after `--` is the server command:

```
claude mcp add --transport stdio job-apply -- uvx --from git+https://github.com/tanmayagrawal21/job-apply-mcp job-apply-mcp
```

Add `--scope user` to make it available in every project, or `--scope project` to write a shared `.mcp.json`
(default scope `local` is private to the current project).

HTTP (start the server first; see [README](README.md)):

```
export JOB_APPLY_MCP_TOKEN=...   # only needed for non-loopback binds
claude mcp add --transport http job-apply http://127.0.0.1:8765/mcp \
  --header "Authorization: Bearer $JOB_APPLY_MCP_TOKEN"
```

Equivalent `.mcp.json` entry:

```json
{
  "mcpServers": {
    "job-apply": {
      "type": "stdio",
      "command": "uvx",
      "args": ["--from", "git+https://github.com/tanmayagrawal21/job-apply-mcp", "job-apply-mcp"]
    }
  }
}
```

Check with `claude mcp list`, or `/mcp` inside a session.

**Not verified:** the Windows-specific `cmd /c` wrapper guidance (the part of the page we read did not include
it). `uvx` is a real executable on Windows, so no wrapper should be needed; `npx`-based servers do need one.

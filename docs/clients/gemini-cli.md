# Gemini CLI

Vendor docs: https://github.com/google-gemini/gemini-cli/blob/main/docs/tools/mcp-server.md  (Verified against the vendor documentation on 2026-10-22.)

User scope `~/.gemini/settings.json`; project scope `.gemini/settings.json`.

```
gemini mcp add -s user job-apply uvx --from git+https://github.com/tanmayagrawal21/job-apply-mcp job-apply-mcp
```

or by hand:

```json
{
  "mcpServers": {
    "job-apply": {
      "command": "uvx",
      "args": ["--from", "git+https://github.com/tanmayagrawal21/job-apply-mcp", "job-apply-mcp"],
      "timeout": 30000
    }
  }
}
```

Streamable HTTP uses **`httpUrl`** (`url` is the SSE endpoint); `$VAR` / `${VAR}` expands in `env`:

```json
{
  "mcpServers": {
    "job-apply": {
      "httpUrl": "http://127.0.0.1:8765/mcp",
      "headers": { "Authorization": "Bearer ${JOB_APPLY_MCP_TOKEN}" }
    }
  }
}
```

Leave `trust` at its default (`false`) so tool calls ask for confirmation.

**Not verified:** whether `headers` values expand environment variables (documented for `env` only).

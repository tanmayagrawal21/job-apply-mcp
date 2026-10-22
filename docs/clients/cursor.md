# Cursor

Vendor docs: https://cursor.com/docs/context/mcp  (Verified against the vendor documentation on 2026-10-22.)

Project: `.cursor/mcp.json`; global: `~/.cursor/mcp.json` (Windows: `%USERPROFILE%\.cursor\mcp.json`).

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

HTTP:

```json
{
  "mcpServers": {
    "job-apply": {
      "url": "http://127.0.0.1:8765/mcp",
      "headers": { "Authorization": "Bearer ${env:JOB_APPLY_MCP_TOKEN}" }
    }
  }
}
```

`${env:NAME}` interpolation is supported in `command`, `args`, `env`, `url` and `headers`.
Reload via Settings -> Tools & MCP.

**Not verified:** the Windows global path (assumed to mirror the documented `~` path).

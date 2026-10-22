# Windsurf

Vendor docs: https://docs.devin.ai/desktop/cascade/mcp (the former docs.windsurf.com page now redirects
here)  (Verified against the vendor documentation on 2026-10-22.)

The Cascade panel's MCP section has an "Open MCP config file" button; use it to find the right file.
The page we read documents `~/.config/devin/mcp_config.json` (Windows `%APPDATA%\devin\mcp_config.json`).
Older Windsurf releases used `~/.codeium/windsurf/mcp_config.json`.

```json
{
  "mcpServers": {
    "job-apply": {
      "command": "uvx",
      "args": ["--from", "git+https://github.com/tanmayagrawal21/job-apply-mcp", "job-apply-mcp"]
    }
  }
}
```

HTTP uses `serverUrl` (or `url`) plus `headers`, with `${env:NAME}` interpolation:

```json
{
  "mcpServers": {
    "job-apply": {
      "serverUrl": "http://127.0.0.1:8765/mcp",
      "headers": { "Authorization": "Bearer ${env:JOB_APPLY_MCP_TOKEN}" }
    }
  }
}
```

**Not verified:** which path your installed Windsurf version reads (the vendor page was rebranded to
"Devin Desktop"); trust the in-app button over any path written here.

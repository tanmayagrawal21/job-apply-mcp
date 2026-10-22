# VS Code (GitHub Copilot agent mode)

Vendor docs: https://code.visualstudio.com/docs/agents/reference/mcp-configuration  (Verified against the vendor documentation on 2026-10-22.)

Workspace file `.vscode/mcp.json` (or the user-profile `mcp.json` via the command palette,
"MCP: Open User Configuration" (the command opens the right file; we did not confirm its path per OS)). VS Code uses a top-level **`servers`** key, not `mcpServers`:

```json
{
  "servers": {
    "job-apply": {
      "type": "stdio",
      "command": "uvx",
      "args": ["--from", "git+https://github.com/tanmayagrawal21/job-apply-mcp", "job-apply-mcp"]
    }
  }
}
```

HTTP with a token prompted at start (never stored in the file):

```json
{
  "inputs": [
    { "type": "promptString", "id": "jam-token", "description": "job-apply-mcp token", "password": true }
  ],
  "servers": {
    "job-apply": {
      "type": "http",
      "url": "http://127.0.0.1:8765/mcp",
      "headers": { "Authorization": "Bearer ${input:jam-token}" }
    }
  }
}
```

Start it from the "Start" CodeLens above the entry, then enable the tools in the Copilot chat tools picker
(Agent mode). The vendor page notes that the portable `.mcp.json` / Copilot `mcp-config.json` files use
`mcpServers` instead.

**Not verified:** the exact on-disk path of the user-profile `mcp.json` per OS.

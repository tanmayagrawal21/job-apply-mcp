# Claude Desktop

Vendor docs: https://modelcontextprotocol.io/quickstart/user  (Verified against the vendor documentation on 2026-10-22.)

Config file (Settings -> Developer -> Edit Config):

- macOS: `~/Library/Application Support/Claude/claude_desktop_config.json`
- Windows: `%APPDATA%\Claude\claude_desktop_config.json`

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

Fully quit and restart Claude Desktop. If you installed with pipx, use `"command": "job-apply-mcp"` and drop
`args` (use the absolute path from `which job-apply-mcp` / `where job-apply-mcp` if the app cannot find it,
because GUI apps often do not inherit your shell PATH). Logs: `~/Library/Logs/Claude` (macOS),
`%APPDATA%\Claude\logs` (Windows).

**Not verified:** remote (HTTP) servers in the config file. Anthropic's guide points to Connectors for remote
servers; for HTTP mode with Claude Desktop use the Connectors UI or a stdio bridge such as `mcp-remote`.
Linux: the same guide lists only macOS and Windows paths.

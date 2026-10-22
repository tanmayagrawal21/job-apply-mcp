# Zed

Vendor docs: https://zed.dev/docs/ai/mcp  (Verified against the vendor documentation on 2026-10-22.)

Run the `zed: open settings file` action and add to `settings.json` (key is **`context_servers`**):

```json
{
  "context_servers": {
    "job-apply": {
      "command": "uvx",
      "args": ["--from", "git+https://github.com/tanmayagrawal21/job-apply-mcp", "job-apply-mcp"],
      "env": {}
    }
  }
}
```

HTTP; if no `Authorization` header is set Zed attempts MCP OAuth, so set one for the bearer token. Zed
does not interpolate environment variables in this file, so the token appears in `settings.json`; prefer the
loopback bind without a token on a single-user machine:

```json
{
  "context_servers": {
    "job-apply": { "url": "http://127.0.0.1:8765/mcp" }
  }
}
```

Enable the server's tools in the Agent panel settings.

**Not verified:** per-OS settings file locations (the page says to use the action above); header
interpolation.

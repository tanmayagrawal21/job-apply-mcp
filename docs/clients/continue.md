# Continue

Vendor docs: https://docs.continue.dev/customize/deep-dives/mcp  (Verified against the vendor documentation on 2026-10-22.)

Create `.continue/mcpServers/job-apply.yaml` in your workspace:

```yaml
name: job-apply
version: 0.0.1
schema: v1
mcpServers:
  - name: job-apply
    type: stdio
    command: uvx
    args:
      - --from
      - git+https://github.com/tanmayagrawal21/job-apply-mcp
      - job-apply-mcp
```

HTTP:

```yaml
name: job-apply-http
version: 0.0.1
schema: v1
mcpServers:
  - name: job-apply
    type: streamable-http
    url: http://127.0.0.1:8765/mcp
```

JSON files in Claude Desktop / Cursor style (`mcpServers` object) dropped into `.continue/mcpServers/`
are also picked up. MCP tools work in **Agent mode** only.

**Not verified:** how to send a bearer header from YAML (the page documents `url` and `env` only); use
the loopback bind without a token, or the stdio config.

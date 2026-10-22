# Generic MCP client

Most clients accept the de-facto `mcpServers` JSON:

```json
{
  "mcpServers": {
    "job-apply": {
      "command": "uvx",
      "args": ["--from", "git+https://github.com/tanmayagrawal21/job-apply-mcp", "job-apply-mcp"],
      "env": {}
    }
  }
}
```

(`env` is optional; delete it if unused. Set `JOB_APPLY__SECTION__KEY=value` variables here to override
config keys, for example `"JOB_APPLY__DRY_RUN": "true"`.)

Other launch commands, equivalent: `job-apply-mcp` (pipx/pip install), `python -m job_apply_mcp`.

Streamable HTTP clients: URL `http://127.0.0.1:8765/mcp`, header `Authorization: Bearer <token>` when the
server was started with `JOB_APPLY_MCP_TOKEN`. `GET /healthz` is an unauthenticated liveness probe.

Smoke test without any client:

```
npx @modelcontextprotocol/inspector uvx --from git+https://github.com/tanmayagrawal21/job-apply-mcp job-apply-mcp
```

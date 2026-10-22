# Client configuration

job-apply-mcp speaks MCP over **stdio** (default; the client launches it) and **streamable HTTP**
(`job-apply-mcp --transport http`, endpoint `http://127.0.0.1:8765/mcp`). Pick the client you use:

| Client | Doc | Config file / command |
|---|---|---|
| Claude Desktop | [claude-desktop.md](claude-desktop.md) | `claude_desktop_config.json` |
| Claude Code | [claude-code.md](claude-code.md) | `claude mcp add` |
| Cursor | [cursor.md](cursor.md) | `.cursor/mcp.json` or `~/.cursor/mcp.json` |
| VS Code (Copilot) | [vscode.md](vscode.md) | `.vscode/mcp.json` |
| Windsurf | [windsurf.md](windsurf.md) | `mcp_config.json` |
| Zed | [zed.md](zed.md) | `settings.json` -> `context_servers` |
| Codex CLI | [codex-cli.md](codex-cli.md) | `~/.codex/config.toml` |
| Gemini CLI | [gemini-cli.md](gemini-cli.md) | `~/.gemini/settings.json` |
| Continue | [continue.md](continue.md) | `.continue/mcpServers/*.yaml` |
| Anything else | [generic.md](generic.md) | generic JSON |

Run `job-apply-mcp init` first; it prints a ready snippet for your machine.

## The launch command

Until the package is on PyPI, launch from the git URL (needs [uv](https://docs.astral.sh/uv/)):

```
uvx --from git+https://github.com/tanmayagrawal21/job-apply-mcp job-apply-mcp
```

After a PyPI release this becomes `uvx job-apply-mcp`. If you installed with `pipx install git+https://github.com/tanmayagrawal21/job-apply-mcp`
or `pip install`, the command is just `job-apply-mcp` (or `python -m job_apply_mcp`). On Windows use the same
commands; if a client cannot find `uvx`, give its full path (`where uvx`).

## HTTP mode

```
job-apply-mcp --transport http --host 127.0.0.1 --port 8765
```

Loopback needs no token. For any other bind address set `JOB_APPLY_MCP_TOKEN` (the server refuses otherwise)
and have the client send `Authorization: Bearer <token>`. Clients that only support stdio can bridge with a
proxy such as `mcp-remote` (not maintained here).

## Verification status

Verified against the vendor documentation on 2026-10-22. Items we could not confirm are listed under "Not verified" in each file. Formats change; if a
snippet stops working, check the vendor link at the top of that page and open an issue.

# ADR-0020: Distribution and usability

Status: accepted · Date: 2026-10-22

## Context
The server only ran from a developer checkout on macOS, with one hand-edited config path and stdio only. Other
people, on other operating systems and MCP clients, need to install, configure and diagnose it without reading
source.

## Decisions
1. **Packaging.** `pyproject.toml` carries PyPI metadata (SPDX license, classifiers, URLs, keywords, `py.typed`);
   the version lives only there and `__version__` reads installed metadata. Console scripts `job-apply-mcp` and
   `job-apply-mcp-init`; `python -m job_apply_mcp`. The `examples/` folder is force-included in the wheel as
   `job_apply_mcp/_examples` so `init` works from `uvx`/`pipx`. No new runtime dependencies (`uvicorn` and
   `starlette` already arrive with `mcp`).
2. **One CLI, backwards compatible.** `job-apply-mcp` with no subcommand still starts stdio, so existing client
   configs keep working. Subcommands: `serve`, `init`, `doctor`, `version`.
3. **`init`** copies the fictional examples (profile, controls) and renders a minimal `config.toml` (only keys that
   differ from defaults, always `dry_run = true`). It probes Ollama (`/api/tags`) and OpenAI-compatible servers
   (:1234 LM Studio, :8080 mlx_lm.server/llama.cpp, :8000) with 0.6 s timeouts, and picks model sizes by RAM
   (<8 GB tiny model, prose by the client; 8-16 GB 4B + 7B; 16 GB+ 4B + 12B). It never downloads models and never
   overwrites without `--force` (then saves `.bak`). Generated TOML is parsed before being written.
4. **`doctor`** reports ok/warn/FAIL with a fix line per problem; exit code 1 only on FAIL. `--offline` skips
   network probes; `--json` for tooling.
5. **HTTP transport** reuses FastMCP's streamable HTTP app wrapped in a tiny ASGI bearer-auth middleware
   (constant-time compare). Loopback by default; a non-loopback bind without `JOB_APPLY_MCP_TOKEN` exits. The token
   comes only from the environment. `/healthz` is the only unauthenticated path. For non-loopback binds the
   built-in DNS-rebinding host allow-list (localhost only) is replaced by `JOB_APPLY_MCP_ALLOWED_HOSTS` or disabled
   when unset, which is acceptable because the bearer token is required.
6. **Cross-platform.** `paths.py` (stdlib) keeps the XDG layout on Linux/macOS and uses `%APPDATA%` /
   `%LOCALAPPDATA%` on Windows; every text read/write is UTF-8; `hf_local` device defaults to `auto`.
   CI matrix: ubuntu, macos, windows x Python 3.11-3.13, plus a wheel-install job, a docker build and the privacy job.
7. **Docs.** Client guides were checked against vendor documentation on 2026-10-22; anything unconfirmed is
   listed under "Not verified". `docs/configuration.md` is generated from the pydantic models and checked in CI.

## Consequences
- Wheel includes example files (small). Config defaults moved behind `default_factory`; POSIX values unchanged.
- The HTTP mode is single-user by design (one token, one profile). It is not a multi-tenant service.
- Client formats drift; the guides carry dates and vendor links so staleness is visible.
- Windows CI could not be run by the author locally; first CI run is the verification.

## Verify / revert
`uv run pytest tests/test_usability.py -q`, `python scripts/gen_config_docs.py --check`, then
`uv build` and install the wheel in a clean venv: `job-apply-mcp init --yes && job-apply-mcp doctor --offline`.
Revert: drop `cli.py`, `doctor.py`, `setup_wizard.py`, `httpserve.py`, `paths.py`, `sysinfo.py`, restore the
`server:main` script entry and the previous `pyproject.toml`, CI file and README (single squash commit).

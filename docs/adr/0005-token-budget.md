# ADR-0005: Minimize tokens crossing the MCP boundary

Status: accepted · Date: 2026-10-22

## Decision
- Tools return **compact JSON**: short keys, no whitespace, no nulls, and refs instead of payloads.
  Descriptions and full forms are opt-in (`fields=[...]`).
- **Pagination** everywhere (`limit`, `cursor`) and a hard `response_budget_chars` cap with an
  explicit truncation marker (see Amendment 1: never by slicing JSON text).
- **Work stays server-side:** screening, gating, dedupe and form-field mapping run in Python or local
  LLMs. Only the irreducible creative text (essays, cover letters) is returned to the client, as
  `client_tasks` with the minimum context needed.
- **Caching:** HTTP responses are cached by URL with ETag/TTL, and form schemas by ref.
- Fewer, more capable tools (8 at launch) to keep the client's tool-list prompt small.

## Verify / revert
Planned (lands with the tool-implementation PR): `tests/test_token_budget.py` asserts every tool's default output for fixture data is under budget.

## Amendment 1 (2026-10-22): schema-safe budgeting and error envelope
Context: `_out()` sliced the serialized string and appended a suffix, producing invalid JSON for large results.
Decision: `core/output.py` shapes the *object* before serializing, always yielding valid compact JSON within
`response_budget_chars`: (1) paginate arrays (keep first N, add `next` cursor and `omitted` count),
(2) truncate long strings with an in-string `…[+N chars]` marker, (3) drop optional verbose fields per a per-tool
priority list (`DROP_PRIORITY`, recorded in `dropped`), (4) aggressive pass, (5) fixed minimal fallback
`{"ok":true,"truncated":true,...}`. All tools registered via `server.mcp.tool` are wrapped (`guarded`) so
success is `{"ok":true,...}` and failure is `{"ok":false,"error":{"code","message","hint"}}`;
NotImplementedError, ComponentMissing, validation/value/key errors and unexpected exceptions map to codes
(`not_implemented`, `component_missing`, `invalid_input`, `not_found`, `internal`, ...) with no traceback.
Consequences: clients can always `json.loads` and branch on `ok`; control tools gain `ok:true` on success.
Arguments rejected by FastMCP before the tool runs still use MCP's own error path.
Verify: `pytest tests/test_output.py` (randomized nested payloads at several budgets; every registered tool's
error path). Revert: restore the old `_out` and remove the `mcp.tool` wrapper in `server.py`.

# ADR-0005: Minimize tokens crossing the MCP boundary

Status: accepted · Date: 2026-10-22

## Decision
- Tools return **compact JSON**: short keys, no whitespace, no nulls, and refs instead of payloads.
  Descriptions and full forms are opt-in (`fields=[...]`).
- **Pagination** everywhere (`limit`, `cursor`) and a hard `response_budget_chars` cap with an
  explicit truncation marker.
- **Work stays server-side:** screening, gating, dedupe and form-field mapping run in Python or local
  LLMs. Only the irreducible creative text (essays, cover letters) is returned to the client, as
  `client_tasks` with the minimum context needed.
- **Caching:** HTTP responses are cached by URL with ETag/TTL, and form schemas by ref.
- Fewer, more capable tools (8 at launch) to keep the client's tool-list prompt small.

## Verify / revert
Planned (lands with the tool-implementation PR): `tests/test_token_budget.py` asserts every tool's default output for fixture data is under budget.

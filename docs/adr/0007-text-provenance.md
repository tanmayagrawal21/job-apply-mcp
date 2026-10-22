# ADR-0007: Text provenance, human verification and export tagging

Status: accepted · Date: 2026-10-22

## Context
Applications contain text from the user and from LLMs. We must know which is which, for honesty
(`policy.stop_phrases` attestations), for auditability, and so later code (e.g. fine-tuning) can use
only text the user authored or approved.

## Decisions
1. **Own SQLite file and table** (`prov_text`) at `provenance.db_path` or `<paths.data_dir>/provenance.db`.
   `store/` is untouched. Stdlib `sqlite3` only, no new dependency; async services wrap it in `asyncio.to_thread`.
2. **Append-only records** `{id, kind, text, origin, model, prompt_hash, parent_id, created_at, verified_by,
   diff_from_parent, context}`. Origins: `user_written | user_verified | user_edited | llm_generated`.
3. **Approval creates a child.** `verify_text(id[, edited_text])` makes a `user_verified` child (text
   unchanged) or a `user_edited` child with a unified diff. The LLM original is never mutated. Only
   `llm_generated` records can be verified, and `record_text` refuses to create `user_verified` or
   `user_edited` directly, so approval cannot be forged. `llm_generated` requires `model`.
4. **Export tagging** is configurable via `provenance.export_tagging`: `none | sidecar (default) | metadata`.
   Sidecar writes `<file>.provenance.json` locally; metadata returns key/values for the caller to embed.
   Nothing is injected into submitted content.
5. **`DatasetSource` Protocol** (`provenance/dataset_source.py`) exposes only `user_written`,
   `user_verified` and `user_edited` records. `llm_generated` is unreachable through it. No training code
   is included in this ADR.
6. Three tools added (`record_text`, `verify_text`, `provenance`), a deliberate exception to ADR-0005's
   "8 tools" target; outputs are compact and the text is only returned with `include_text=True`.

## Consequences
Lineage is auditable; verification is explicit. `context` (what the text answers) is stored so a future
dataset can form instruction/response pairs. Texts may contain personal data and live only in the local DB.

## Verify / revert
Verify: `uv run ruff check . && uv run pytest -q tests/test_provenance.py`.
Revert: `git revert` the squash commit; delete `provenance.db` (it is not referenced elsewhere).

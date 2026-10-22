"""Dataset source for tuning (ADR-0017). Codes against the provenance `DatasetSource` (ADR-0007, PR #7).

If the provenance package is absent (PR #7 not merged yet) a structurally identical local fallback is used, so
this package works and is testable either way. Either way ONLY user_written / user_verified / user_edited
records are reachable; `llm_generated` text can never enter a dataset.
"""
from __future__ import annotations

from typing import Any, Protocol, runtime_checkable

from pydantic import BaseModel

TRUSTED = frozenset({"user_written", "user_verified", "user_edited"})

try:  # pragma: no cover - exercised once PR #7 is on main
    from ..provenance import DatasetSource, ProvenanceRecord  # type: ignore[attr-defined]
except ImportError:
    class ProvenanceRecord(BaseModel):  # type: ignore[no-redef]
        id: str
        kind: str
        text: str
        origin: str
        created_at: str = ""
        parent_id: str | None = None
        diff_from_parent: str | None = None
        context: str | None = None

    @runtime_checkable
    class DatasetSource(Protocol):  # type: ignore[no-redef]
        def items(self, kinds: list[str] | None = None) -> list[Any]: ...


def origin_of(rec: Any) -> str:
    o = rec.origin
    return getattr(o, "value", o)


def trusted_items(source: Any, kinds: list[str] | None = None) -> list[Any]:
    """Defense in depth: even a buggy source cannot smuggle llm_generated text into training."""
    return [r for r in source.items(kinds) if origin_of(r) in TRUSTED and r.text and r.text.strip()]

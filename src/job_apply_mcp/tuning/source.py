"""Dataset source for tuning (ADR-0017): the provenance `DatasetSource` (ADR-0007).

Only user_written / user_verified / user_edited records may be trained on. Rather than silently filtering, a
source that yields anything else is REFUSED, so a mislabeled or leaking source is loud, not quiet.
"""
from __future__ import annotations

from typing import Any

from ..provenance import USER_TRUSTED, DatasetSource, ProvenanceRecord

TRUSTED = frozenset(o.value for o in USER_TRUSTED)

__all__ = ["TRUSTED", "DatasetSource", "ProvenanceRecord", "TuningRefused", "origin_of", "trusted_items"]


class TuningRefused(RuntimeError):
    """Training input failed the provenance guard."""


def origin_of(rec: Any) -> str:
    o = rec.origin
    return getattr(o, "value", o)


def trusted_items(source: Any, kinds: list[str] | None = None) -> list[Any]:
    """Every item must carry a trusted origin; any other origin aborts (defense in depth over ADR-0007)."""
    items = list(source.items(kinds))
    bad = sorted({f"{r.id}:{origin_of(r)}" for r in items if origin_of(r) not in TRUSTED})
    if bad:
        raise TuningRefused(f"untrusted origin in training input, refusing to train: {bad[:5]}")
    return [r for r in items if r.text and r.text.strip()]

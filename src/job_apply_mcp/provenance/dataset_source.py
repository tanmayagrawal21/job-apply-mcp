"""Minimal interface for later code (e.g. fine-tuning) to read ONLY user-authored text (ADR-0007).

llm_generated records are unreachable through this interface by construction.
"""
from __future__ import annotations

from typing import Protocol, runtime_checkable

from .models import USER_TRUSTED, ProvenanceRecord
from .store import ProvenanceStore


@runtime_checkable
class DatasetSource(Protocol):
    def items(self, kinds: list[str] | None = None) -> list[ProvenanceRecord]:
        """Records whose origin is user_written, user_verified or user_edited."""


class ProvenanceDatasetSource:
    def __init__(self, store: ProvenanceStore):
        self._store = store

    def items(self, kinds: list[str] | None = None) -> list[ProvenanceRecord]:
        rows = self._store.list_by_origin(USER_TRUSTED, kinds)
        assert all(r.origin in USER_TRUSTED for r in rows)
        return rows

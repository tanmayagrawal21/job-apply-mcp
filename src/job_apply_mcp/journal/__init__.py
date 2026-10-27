"""Candidate journal (ADR-0064): a dated, append-only log of what the candidate said about themselves.

Any connected client records facts with `record_candidate_fact` as they come up in conversation. Capture is never
blocked; gating happens when a fact is USED to fill a form (`overlay.JournalView`).
"""
from __future__ import annotations

from .store import (
    CandidateJournal,
    Entry,
    redact_secrets,
    risk_class,
)

__all__ = ["CandidateJournal", "Entry", "redact_secrets", "risk_class"]

"""Provenance data model (ADR-0007)."""
from __future__ import annotations

from enum import Enum
from typing import Any

from pydantic import BaseModel


class Origin(str, Enum):
    user_written = "user_written"
    user_verified = "user_verified"   # LLM text the user approved unchanged
    user_edited = "user_edited"       # LLM text the user changed (diff_from_parent is set)
    llm_generated = "llm_generated"   # not yet approved by the user


USER_TRUSTED = frozenset({Origin.user_written, Origin.user_verified, Origin.user_edited})


class ProvenanceRecord(BaseModel):
    id: str
    kind: str                         # resume_bullet | cover_letter | answer | profile_fact | ...
    text: str
    origin: Origin
    model: str | None = None
    prompt_hash: str | None = None
    parent_id: str | None = None
    created_at: str
    verified_by: str | None = None
    diff_from_parent: str | None = None
    context: str | None = None        # what the text answers (job description, question); used as SFT instruction

    @property
    def llm_generated(self) -> bool:
        return self.origin is Origin.llm_generated

    def compact(self) -> dict[str, Any]:
        d: dict[str, Any] = {"id": self.id, "kind": self.kind, "origin": self.origin.value,
                             "at": self.created_at, "chars": len(self.text)}
        for k, v in (("model", self.model), ("prompt", self.prompt_hash), ("parent", self.parent_id),
                     ("by", self.verified_by)):
            if v:
                d[k] = v
        return d

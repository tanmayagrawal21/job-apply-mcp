"""Shared data models. Kept small and flat so tool responses stay token-cheap.

Every model has a `compact()` view that tools return by default; full views are opt-in via `fields=`.
"""
from __future__ import annotations

from enum import Enum
from typing import Any

from pydantic import BaseModel, Field


class FieldKind(str, Enum):
    text = "text"
    textarea = "textarea"
    select = "select"
    multiselect = "multiselect"
    boolean = "boolean"
    file = "file"
    date = "date"
    number = "number"
    location = "location"
    unknown = "unknown"


class FormField(BaseModel):
    key: str
    label: str
    kind: FieldKind = FieldKind.unknown
    required: bool = False
    options: list[str] = Field(default_factory=list)
    max_length: int | None = None


class Posting(BaseModel):
    """A job posting normalized across platforms. `ref` is the stable id: '<platform>:<board>:<job_id>'."""

    ref: str
    platform: str
    company: str
    title: str
    location: str = ""
    url: str
    apply_url: str | None = None
    posted_at: str | None = None
    salary: str | None = None
    description: str | None = None  # only populated when explicitly requested
    flags: dict[str, list[str]] = Field(default_factory=dict)  # policy flags found in the text

    def compact(self) -> dict[str, Any]:
        d = {"ref": self.ref, "co": self.company, "title": self.title, "loc": self.location}
        if self.salary:
            d["pay"] = self.salary
        if self.flags:
            d["flags"] = sorted(self.flags)
        return d


class ApplicationForm(BaseModel):
    ref: str
    fields: list[FormField]
    submit_via: list[str] = Field(default_factory=list)  # e.g. ["api", "browser"] in preference order


class Decision(str, Enum):
    submit = "submit"
    skip = "skip"
    needs_human = "needs_human"


class GateResult(BaseModel):
    decision: Decision
    reasons: list[str] = Field(default_factory=list)


class Answer(BaseModel):
    key: str
    value: Any
    source: str  # "profile" | "rule" | "llm:<model>" | "client"
    confidence: float = 1.0


class ApplicationPlan(BaseModel):
    ref: str
    gate: GateResult
    answers: list[Answer] = Field(default_factory=list)
    unresolved: list[str] = Field(default_factory=list)  # field keys that still need a human or the client LLM
    audit: dict[str, Any] = Field(default_factory=dict)  # deterministic-rule computations (e.g. salary)


class SubmitResult(BaseModel):
    ref: str
    status: str  # submitted | duplicate | incomplete | needs_human | roadblock | error | dry_run | handoff | awaiting_code
    via: str | None = None
    confirmation: str | None = None
    detail: str | None = None
    pending_id: str | None = None   # awaiting_code only: pass to provide_verification_code (ADR-0026)
    client_tasks: list[dict[str, Any]] = Field(default_factory=list)   # needs_review: prose that failed review (ADR-0044)

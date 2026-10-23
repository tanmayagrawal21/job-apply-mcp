"""The `[persona]` profile section (ADR-0047): policies that decide *how* facts are counted and chosen.

Every field has a safe default and records whether the owner set it (`model_fields_set`), so the tool can tell a
configured policy from a default-used one and ask about the latter. A bare string is accepted for each policy and
kept verbatim as `note` (free text the writer may read); the structured knobs then keep their defaults. Nothing in
a note is parsed or guessed at.
"""
from __future__ import annotations

import re
from typing import Any, Literal

from pydantic import BaseModel, Field, model_validator


class _NoteOrTable(BaseModel):
    """A bare string in TOML becomes `note`; a table is read field by field."""

    @model_validator(mode="before")
    @classmethod
    def _bare_string(cls, data: Any) -> Any:
        return {"note": data.strip()} if isinstance(data, str) else data


class ExperienceCounting(_NoteOrTable):
    """Which roles count as professional experience, and from when."""
    mode: Literal["full_time_only", "include_internships", "all"] = "full_time_only"
    count_from: str = ""                      # YYYY-MM: roles starting earlier do not count ("" = no limit)
    include_orgs: list[str] = Field(default_factory=list)   # always count these employers
    exclude_orgs: list[str] = Field(default_factory=list)   # never count these employers
    note: str = ""


class SalaryPolicy(_NoteOrTable):
    """Default: the midpoint of the posted range, never below the floor. With no range: `fixed`, else ask."""
    mode: Literal["midpoint", "floor", "top"] = "midpoint"
    floor: int | None = None                  # never answer below this (annual)
    fixed: int | None = None                  # figure to enter when the posting shows no range
    note: str = ""


class Project(BaseModel):
    name: str
    summary: str = ""
    keywords: list[str] = Field(default_factory=list)   # topics this project is the best example of
    url: str = ""


class ExceptionalWork(_NoteOrTable):
    """Projects to pick from for 'exceptional work' questions; the most relevant one is chosen per posting."""
    projects: list[Project] = Field(default_factory=list)
    use_resume_projects: bool = True          # also consider the resume's project facts
    note: str = ""


class Persona(BaseModel):
    experience_counting: ExperienceCounting = Field(default_factory=ExperienceCounting)
    salary_policy: SalaryPolicy = Field(default_factory=SalaryPolicy)
    exceptional_work: ExceptionalWork = Field(default_factory=ExceptionalWork)
    # Reusable free-form answers (AI tools used, working style, ...): canonical key or question pattern -> text.
    answers: dict[str, str] = Field(default_factory=dict)

    def configured(self, name: str) -> bool:
        """True when the owner wrote this policy in the profile (a bare string counts)."""
        return name in self.model_fields_set


_WORD = re.compile(r"[a-z0-9+#]{3,}")


def words(text: str) -> set[str]:
    return set(_WORD.findall(text.lower()))


def pick_project(projects: list[Project], posting_text: str) -> Project | None:
    """The project whose name/summary/keywords overlap most with the posting; ties keep the owner's order.
    With no overlap at all the first project is returned (the owner lists the strongest first)."""
    if not projects:
        return None
    want = words(posting_text)
    best, best_score = projects[0], -1
    for p in projects:
        score = 2 * len(want & words(" ".join(p.keywords))) + len(want & words(f"{p.name} {p.summary}"))
        if score > best_score:
            best, best_score = p, score
    return best

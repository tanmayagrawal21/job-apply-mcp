"""Candidate profile loaded from TOML (contact, links, education, work, skills, answers, demographics).

Standard answers are keyed by *normalized question patterns*: a lowercase, punctuation-free substring of
the form label (longest match wins), or `re:<regex>` for full control. The profile never lives in the
repo; see examples/profile.example.toml (fictional person).
"""
from __future__ import annotations

import re
import tomllib
from pathlib import Path

from pydantic import BaseModel, Field

DECLINE = "Decline to self-identify"


class Contact(BaseModel):
    first_name: str = ""
    last_name: str = ""
    email: str = ""
    phone: str = ""
    city: str = ""
    region: str = ""
    country: str = ""
    postal_code: str = ""

    @property
    def full_name(self) -> str:
        return f"{self.first_name} {self.last_name}".strip()


class Education(BaseModel):
    school: str
    degree: str = ""
    field: str = ""
    start: str = ""          # YYYY-MM
    end: str = ""            # YYYY-MM (expected graduation if in the future)
    gpa: str = ""


class Work(BaseModel):
    company: str
    title: str
    start: str = ""
    end: str = ""            # empty = current
    location: str = ""
    summary: str = ""


class Demographics(BaseModel):
    """Every field defaults to declining to answer."""
    gender: str = DECLINE
    race_ethnicity: str = DECLINE
    veteran_status: str = DECLINE
    disability_status: str = DECLINE
    sexual_orientation: str = DECLINE
    pronouns: str = DECLINE


class SalaryRules(BaseModel):
    expected: int | None = None         # annual figure to enter where a number is required
    text: str = "Open to discussion based on the full scope of the role."
    currency: str = "USD"


class Profile(BaseModel):
    contact: Contact = Field(default_factory=Contact)
    links: dict[str, str] = Field(default_factory=dict)     # linkedin, github, website, ...
    education: list[Education] = Field(default_factory=list)
    work: list[Work] = Field(default_factory=list)
    skills: list[str] = Field(default_factory=list)
    answers: dict[str, str] = Field(default_factory=dict)
    demographics: Demographics = Field(default_factory=Demographics)
    salary: SalaryRules = Field(default_factory=SalaryRules)

    def graduation(self) -> str | None:
        """Latest education end date (YYYY-MM), for PolicyCfg.graduation."""
        ends = sorted(e.end for e in self.education if e.end)
        return ends[-1] if ends else None

    def answer_for(self, question: str) -> str | None:
        q = normalize_question(question)
        best: tuple[int, str] | None = None
        for pat, val in self.answers.items():
            if pat.startswith("re:"):
                if re.search(pat[3:], question, re.IGNORECASE) and (best is None or len(pat) > best[0]):
                    best = (len(pat), val)
                continue
            p = normalize_question(pat)
            if p and p in q and (best is None or len(p) > best[0]):
                best = (len(p), val)
        return best[1] if best else None

    def demographic_for(self, question: str) -> str | None:
        """Answer for EEO-style questions; declines by default. None if not a demographic question."""
        q = normalize_question(question)
        table = {
            "gender": self.demographics.gender, "race": self.demographics.race_ethnicity,
            "ethnic": self.demographics.race_ethnicity, "veteran": self.demographics.veteran_status,
            "disabilit": self.demographics.disability_status,
            "sexual orientation": self.demographics.sexual_orientation,
            "pronoun": self.demographics.pronouns,
        }
        for k, v in table.items():
            if k in q:
                return v
        return None


def normalize_question(s: str) -> str:
    return " ".join(re.sub(r"[^a-z0-9 ]+", " ", s.lower()).split())


def load_profile(path: str | Path) -> Profile:
    p = Path(path).expanduser()
    if not p.is_file():
        raise FileNotFoundError(f"profile not found: {p} (copy examples/profile.example.toml)")
    return Profile.model_validate(tomllib.loads(p.read_text()))

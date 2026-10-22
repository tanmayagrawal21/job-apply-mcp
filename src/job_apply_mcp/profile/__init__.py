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
    lat: float | None = None       # optional coordinates of the configured location (derived geo fields)
    lon: float | None = None

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


class Preferences(BaseModel):
    """Structured yes/no preferences. None = not configured, so the matching question stays unresolved."""
    willing_to_relocate: bool | None = None
    needs_relocation_support: bool | None = None
    open_to_hybrid: bool | None = None
    open_to_onsite: bool | None = None
    # ADR-0034: True lets the mapper acknowledge applicant privacy notices on the owner's behalf. Default off.
    acknowledge_privacy_notices: bool | None = None


class Authorization(BaseModel):
    """Work-authorization facts. None = not configured (falls back to [answers] patterns)."""
    work_authorized: bool | None = None
    requires_sponsorship: bool | None = None
    citizen: bool | None = None            # US citizenship
    citizenship: str = ""                  # country of citizenship; drives "Citizenship Status" and, if citizen is unset, US-citizen


class Profile(BaseModel):
    contact: Contact = Field(default_factory=Contact)
    links: dict[str, str] = Field(default_factory=dict)     # linkedin, github, website, ...
    education: list[Education] = Field(default_factory=list)
    work: list[Work] = Field(default_factory=list)
    skills: list[str] = Field(default_factory=list)
    skill_years: dict[str, float] = Field(default_factory=dict)  # per-technology years; only what you set is ever answered
    languages: list[str] = Field(default_factory=list)           # programming languages, strongest first
    referrals: dict[str, str] = Field(default_factory=dict)      # company -> referrer name (else "know anyone" = No)
    preferences: Preferences = Field(default_factory=Preferences)
    authorization: Authorization = Field(default_factory=Authorization)
    answers: dict[str, str] = Field(default_factory=dict)
    demographics: Demographics = Field(default_factory=Demographics)
    salary: SalaryRules = Field(default_factory=SalaryRules)
    # Groups of interchangeable phrases for [answers] pattern matching (ADR-0011 Amendment 2). Extends the defaults.
    answer_synonyms: list[list[str]] = Field(default_factory=list)
    answer_synonyms_mode: str = "extend"   # "extend" adds to the defaults; "replace" uses only answer_synonyms ([] = off)

    def education_rows(self) -> list[dict[str, str]]:
        """Generic education list for ATS education blocks, most recent first (ADR-0029). Years are 4-digit strings."""
        def yr(v: str) -> str:
            return v[:4] if re.fullmatch(r"\d{4}(-\d{2})?", v or "") else ""
        rows = sorted(self.education, key=lambda e: e.end or "9999", reverse=True)
        return [{"school": e.school, "degree": e.degree, "discipline": e.field, "start_year": yr(e.start),
                 "end_year": yr(e.end), "start_month": (e.start[5:7] if len(e.start) >= 7 else ""),
                 "end_month": (e.end[5:7] if len(e.end) >= 7 else ""), "gpa": e.gpa} for e in rows]

    def graduation(self) -> str | None:
        """Latest education end date (YYYY-MM), for PolicyCfg.graduation."""
        ends = sorted(e.end for e in self.education if e.end)
        return ends[-1] if ends else None

    def answer_for(self, question: str) -> str | None:
        q = normalize_question(question)
        groups = [[normalize_question(x) for x in g] for g in (self.answer_synonyms if self.answer_synonyms_mode == "replace"
                                              else (*DEFAULT_ANSWER_SYNONYMS, *self.answer_synonyms))]
        best: tuple[int, str] | None = None
        for pat, val in self.answers.items():
            if pat.startswith("re:"):
                if re.search(pat[3:], question, re.IGNORECASE) and (best is None or len(pat) > best[0]):
                    best = (len(pat), val)
                continue
            p = normalize_question(pat)
            if p and (re.search(rf"\b{re.escape(p)}\b", q) or _synonym_hit(q, p, groups)) and (best is None or len(p) > best[0]):
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


DEFAULT_ANSWER_SYNONYMS: list[list[str]] = [
    ["in office", "on-site", "onsite", "on site", "in person office", "in-person office", "in person work",
     "in-person work", "office culture", "in the office", "days in office", "days in person"],
    ["remote", "work from home", "wfh", "fully distributed"],
]


_NON_ARRANGEMENT = re.compile(r"\b(interview\w*|onboarding|events?|meetings?|training|conference|orientation)\b")


def _synonym_hit(q: str, p: str, groups: list[list[str]]) -> bool:
    """True when `p` belongs to a synonym group and another member of that group appears in the question."""
    if _NON_ARRANGEMENT.search(q):  # interviews/onboarding/events are not a work arrangement
        return False
    for g in groups:
        if p in g and any(m and re.search(rf"\b{re.escape(m)}\b", q) for m in g):
            return True
    return False


def load_profile(path: str | Path) -> Profile:
    p = Path(path).expanduser()
    if not p.is_file():
        raise FileNotFoundError(f"profile not found: {p} (copy examples/profile.example.toml)")
    return Profile.model_validate(tomllib.loads(p.read_text(encoding="utf-8")))

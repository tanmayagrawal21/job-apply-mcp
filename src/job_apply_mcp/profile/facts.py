"""Fact precedence and resume-first resolution (ADR-0023).

Precedence: resume (1) > profile facts and non-conflicting user-letter facts (2) > everything else (never used).
Writers receive ONLY `FactSet.lines`. Letter facts that conflict with, or are outdated relative to, the resume
(employer, title, dates, metrics, status) are excluded and listed in `facts_report()` with the reason.
"""
from __future__ import annotations

import re
from datetime import date
from typing import Any

from pydantic import BaseModel, Field

from .resume import Fact, completed_education, is_past, norm, temporal_phrasing, today_local

_SENT = re.compile(r"(?<=[.!?])\s+|\n+")
_YEAR = re.compile(r"\b(?:19|20)\d{2}\b")
_NUM = re.compile(r"\d[\d,.]*\s?(?:%|x\b|k\b)?")
_TITLE_AT = re.compile(r"\b(?:as|was|worked as|serving as|served as|role as)\s+(?:an?\s+|the\s+)?"
                       r"([A-Z][A-Za-z&/\- ]{2,50}?)\s+(?:at|with|for)\s+([A-Z][\w&.\-]*(?:\s+[A-Z][\w&.\-]*){0,4})")
_STATUS_NOW = re.compile(r"\b(currently|presently|am now)\s+(?:\w+\s+){0,2}?(pursuing|completing|enrolled|studying|finishing|"
                         r"working toward)\b|\b(?:am|I'm)\s+(?:a\s+)?(?:current\s+)?(?:graduate\s+)?student\b|"
                         r"\b(?:expected|will|set to) (?:graduate|complete)\b|\bcandidate for\b", re.IGNORECASE)
_WORK_NOW = re.compile(r"\b(?:currently|presently)\s+(?:\w+\s+){0,2}?(?:work|working|serve|serving|employed)\b|"
                       r"\bin my current role\b", re.IGNORECASE)
_STOP = {"the", "and", "for", "with", "that", "this", "from", "into", "have", "has", "was", "were", "built", "using"}


class Excluded(BaseModel):
    text: str
    reason: str
    correction: str = ""


class LetterFact(BaseModel):
    text: str
    origin: str = "user_written"
    source: str = "letter"
    precedence: int = 2


class Conflict(BaseModel):
    kind: str          # status | title | date | metric
    detail: str


class FactSet(BaseModel):
    resume: list[Fact] = Field(default_factory=list)
    profile_lines: list[str] = Field(default_factory=list)
    letter_facts: list[LetterFact] = Field(default_factory=list)
    excluded: list[Excluded] = Field(default_factory=list)
    conflicts: list[Conflict] = Field(default_factory=list)
    today: str = ""

    @property
    def lines(self) -> list[str]:
        t = date.fromisoformat(self.today) if self.today else today_local()
        return ([f.line() for f in self.resume] + self.profile_lines + [f.text for f in self.letter_facts]
                + temporal_phrasing(self.resume, t))


def _words(s: str) -> set[str]:
    return {w for w in norm(s).split() if len(w) > 3 and w not in _STOP}


def _org_entries(resume: list[Fact], sentence: str) -> list[Fact]:
    n = norm(sentence)
    return [f for f in resume if f.org and norm(f.org) in n]


def check_letter_sentence(s: str, resume: list[Fact], today: date) -> Excluded | None:
    """None when the sentence does not contradict the resume; otherwise the exclusion with its correction."""
    for f in _org_entries(resume, s):
        who = f"{f.title} at {f.org}"
        m = _TITLE_AT.search(s)
        if m and norm(m.group(2)) in norm(f.org) + " " and norm(m.group(1)) != norm(f.title):
            return Excluded(text=s, reason=f"title conflict: letter says '{m.group(1)}', resume says '{f.title}'",
                            correction=who)
        years = [int(y) for y in _YEAR.findall(s)]
        if years and f.start:
            lo = int(f.start[:4])
            hi = int(f.end[:4]) if f.end else today.year
            bad = [y for y in years if y < lo or y > hi]
            if bad:
                span = f"{f.start} to {'present' if f.current else f.end}"
                return Excluded(text=s, reason=f"date conflict: {bad[0]} outside resume span {span}",
                                correction=f"{who} ({span})")
        if not f.current and _WORK_NOW.search(s) and f.end:
            return Excluded(text=s, reason=f"outdated status: resume shows {who} ended {f.end}",
                            correction=f"{who} ended {f.end}; use past tense")
    if _STATUS_NOW.search(s):
        done = completed_education(resume, today)
        if done and not any(f.section == "education" and (f.current or not is_past(f.end, today)) for f in resume):
            e = max(done, key=lambda f: f.end)
            return Excluded(text=s, reason=f"outdated status: resume shows {e.title} completed {e.end}",
                            correction=f"{e.title}, {e.org} (completed {e.end})")
    # metric conflict: same claim (>=3 shared content words with a resume bullet) but different numbers
    sn = {x.strip() for x in _NUM.findall(s) if len(x.strip()) > 1 and not _YEAR.fullmatch(x.strip())}
    if sn:
        for f in resume:
            for b in f.bullets:
                if len(_words(s) & _words(b)) >= 3:
                    bn = {x.strip() for x in _NUM.findall(b) if len(x.strip()) > 1}
                    su, bu = {x for x in sn if re.search(r"[%xk]$", x)}, {x for x in bn if re.search(r"[%xk]$", x)}
                    if (su and bu and not (su & bu)) or (not (su and bu) and bn and not (sn & bn)):
                        return Excluded(text=s, reason=f"metric conflict: letter {sorted(sn)}, resume {sorted(bn)}",
                                        correction=b)
    return None


def admit_letter_facts(letter: str, resume: list[Fact], today: date) -> tuple[list[LetterFact], list[Excluded]]:
    """Concrete sentences (numbers, named systems, projects) from a user-written letter. Additive facts are
    admitted at precedence 2; anything contradicting or outdated vs the resume is excluded with a reason."""
    ok: list[LetterFact] = []
    bad: list[Excluded] = []
    for raw in _SENT.split(letter):
        s = raw.strip()
        if len(s) < 25 or s.lower().startswith("dear") or not (_NUM.search(s) or _STATUS_NOW.search(s)
                                                              or _WORK_NOW.search(s) or _TITLE_AT.search(s)
                                                              or re.search(r"\b(?:built|designed|developed|led|created|"
                                                                           r"implemented|automated|reduced|improved)\b", s, re.IGNORECASE)):
            continue
        ex = check_letter_sentence(s, resume, today) if resume else None
        if ex:
            bad.append(ex)
        elif _STATUS_NOW.search(s) or _WORK_NOW.search(s):
            continue  # status claims are only trustworthy when the resume confirms them; never admitted alone
        else:
            ok.append(LetterFact(text=s))
    return ok, bad


def profile_lines(profile: Any) -> list[str]:
    rows: list[str] = []
    for w in profile.work:
        rows.append(f"{w.title} at {w.company} ({w.start}-{w.end or 'present'}): {w.summary}".strip(": "))
    for e in profile.education:
        rows.append(f"{e.degree} in {e.field}, {e.school} (to {e.end})".replace("  ", " "))
    if profile.skills:
        rows.append("Skills: " + ", ".join(profile.skills))
    return rows


def resume_consistency(resume: list[Fact], profile: Any, today: date | None = None) -> list[Conflict]:
    """Profile vs resume conflicts. The resume wins; each conflict says what the resume shows."""
    today = today or today_local()
    out: list[Conflict] = []
    if not resume or profile is None:
        return out
    for e in profile.education:
        for f in resume:
            if f.section != "education" or norm(f.org) != norm(e.school) or not f.end:
                continue
            if is_past(f.end, today) and (not e.end or not is_past(e.end, today)):
                out.append(Conflict(kind="status", detail=f"profile says {e.school} {e.degree} in progress "
                                    f"(end '{e.end or 'none'}') but resume shows it completed {f.end}"))
            if e.end and e.end != f.end:
                out.append(Conflict(kind="date", detail=f"profile {e.school} end {e.end} vs resume {f.end}"))
    for w in profile.work:
        for f in resume:
            if f.section != "experience" or norm(f.org) != norm(w.company):
                continue
            if norm(f.title) != norm(w.title):
                out.append(Conflict(kind="title", detail=f"profile '{w.title}' at {w.company} vs resume '{f.title}'"))
            if (w.end or "") != (f.end or "") and (w.start or "") == (f.start or ""):
                out.append(Conflict(kind="date", detail=f"profile {w.company} end '{w.end or 'present'}' "
                                    f"vs resume '{f.end or 'present'}'"))
    return out


def _drop_conflicting(profile: Any, resume: list[Fact]) -> list[str]:
    """Profile lines for entries the resume already covers are superseded by the resume (precedence 1)."""
    resume_orgs = {norm(f.org) for f in resume if f.org}
    keep = profile.model_copy(deep=True)
    keep.work = [w for w in keep.work if norm(w.company) not in resume_orgs]
    keep.education = [e for e in keep.education if norm(e.school) not in resume_orgs]
    return profile_lines(keep)


def resolve_facts(resume: list[Fact], profile: Any = None, letters: list[str] | None = None,
                  today: date | None = None) -> FactSet:
    today = today or today_local()
    fs = FactSet(resume=resume, today=today.isoformat())
    fs.conflicts = resume_consistency(resume, profile, today)
    if profile is not None:
        fs.profile_lines = _drop_conflicting(profile, resume) if resume else profile_lines(profile)
    seen: set[str] = set()
    for text in letters or []:
        ok, bad = admit_letter_facts(text, resume, today)
        for f in ok:
            if norm(f.text) not in seen:
                seen.add(norm(f.text))
                fs.letter_facts.append(f)
        fs.excluded += bad
    return fs


def facts_report(fs: FactSet, limit: int = 8) -> dict[str, Any]:
    """Compact report: counts, conflicts and every excluded letter fact with its reason."""
    return {
        "resume_facts": len(fs.resume), "profile_facts": len(fs.profile_lines),
        "letter_facts_admitted": len(fs.letter_facts), "letter_facts_excluded": len(fs.excluded),
        "conflicts": [c.model_dump() for c in fs.conflicts[:limit]],
        "excluded": [{"text": e.text[:140], "reason": e.reason, "use": e.correction[:140]} for e in fs.excluded[:limit]],
        "precedence": "resume(1) > profile/letters(2); conflicts resolved to resume",
    }

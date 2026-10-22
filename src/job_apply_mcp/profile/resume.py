"""Resume ingestion (ADR-0023): the resume is the source of truth for facts and timelines.

Parses PDF (optional `pypdf` extra) or text-like files (.txt/.md/.docx-as-text) into structured facts
{section, org, title, start, end/current, bullets, metrics}. Every fact carries provenance
origin=user_written, source=resume, precedence=1 (the highest).
"""
from __future__ import annotations

import json
import re
from datetime import date, datetime
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, Field

Section = Literal["experience", "education", "projects", "skills", "other"]

_MONTHS = {m: i for i, m in enumerate(
    ["jan", "feb", "mar", "apr", "may", "jun", "jul", "aug", "sep", "oct", "nov", "dec"], 1)}
_DATE = (r"(?:(?:jan|feb|mar|apr|may|jun|jul|aug|sep|sept|oct|nov|dec)[a-z]*\.?\s+\d{4}|\d{1,2}/\d{4}|"
         r"\d{4}-\d{2}|\d{4})")
_RANGE = re.compile(rf"(?P<s>{_DATE})\s*(?:-|–|—|to)\s*(?P<e>{_DATE}|present|current|now|ongoing)",
                    re.IGNORECASE)
_SECTION_WORDS = {
    "experience": ("experience", "employment", "work history", "professional"),
    "education": ("education", "academic"),
    "projects": ("projects", "research"),
    "skills": ("skills", "technologies", "tools"),
}
_ORG_HINT = re.compile(r"\b(inc|llc|ltd|corp|co|company|labs?|university|college|institute|school|group|"
                       r"technologies|systems|partners|analytics|robotics|center|centre)\b\.?", re.IGNORECASE)
_METRIC = re.compile(r"\$\s?\d[\d,.]*\s?[kmb]?\b|\b\d[\d,.]*\s?(?:%|x\b|k\b|m\b|\+)|\b\d{2,}[\d,.]*\b")
_BULLET = re.compile(r"^\s*(?:[-*•▪●·]|\d+[.)])\s+")


class Fact(BaseModel):
    section: Section = "other"
    org: str = ""
    title: str = ""
    start: str = ""                 # YYYY-MM
    end: str = ""                   # YYYY-MM; empty when current
    current: bool = False
    bullets: list[str] = Field(default_factory=list)
    metrics: list[str] = Field(default_factory=list)
    origin: str = "user_written"
    source: str = "resume"
    precedence: int = 1

    def line(self) -> str:
        span = f" ({self.start or '?'} to {'present' if self.current else (self.end or '?')})" if self.start or self.end or self.current else ""
        head = ", ".join(x for x in (self.title, self.org) if x)
        body = "; ".join(self.bullets[:3])
        return (f"{head}{span}" + (f": {body}" if body else "")).strip()[:400]


def norm(s: str) -> str:
    return " ".join(re.sub(r"[^a-z0-9 ]+", " ", s.lower()).split())


def parse_date(raw: str, *, end: bool = False) -> str:
    """Normalise 'Jun 2024' | '06/2024' | '2024-06' | '2024' to YYYY-MM ('' for present-like words)."""
    s = raw.strip().lower().rstrip(".")
    if not s or s in {"present", "current", "now", "ongoing"}:
        return ""
    if m := re.fullmatch(r"(\d{4})-(\d{2})", s):
        return s
    if m := re.fullmatch(r"(\d{1,2})/(\d{4})", s):
        return f"{m.group(2)}-{int(m.group(1)):02d}"
    if m := re.fullmatch(r"([a-z]+)\.?\s+(\d{4})", s):
        mon = _MONTHS.get(m.group(1)[:3])
        if mon:
            return f"{m.group(2)}-{mon:02d}"
    if m := re.fullmatch(r"(\d{4})", s):
        return f"{s}-12" if end else f"{s}-01"
    return ""


def _section_of(line: str) -> Section | None:
    t = line.strip().strip(":").lower()
    if not t or len(t) > 40 or _RANGE.search(t) or _BULLET.match(line):
        return None
    if not (line.strip().isupper() or line.strip().endswith(":") or line.lstrip().startswith("#") or len(t.split()) <= 3):
        return None
    t = t.lstrip("# ").strip()
    for sec, words in _SECTION_WORDS.items():
        if any(t == w or t.startswith(w) for w in words):
            return sec  # type: ignore[return-value]
    return None


def _split_head(head: str, section: str) -> tuple[str, str]:
    """(title, org) from 'Title, Org' | 'Org | Title' | 'Title at Org'. Orgs are recognised by name hints."""
    head = head.strip(" |,-–—\t")
    parts = [p.strip() for p in re.split(r"\s+\|\s+|\s+at\s+|\s+[-–—]\s+|,\s+", head) if p.strip()]
    if len(parts) == 1:
        return parts[0], ""
    a, b = parts[0], parts[1]
    if section == "education":
        # degree first, school second, unless the first looks like a school and the second does not
        if _ORG_HINT.search(a) and not _ORG_HINT.search(b):
            return b, a
        return a, b
    if _ORG_HINT.search(a) and not _ORG_HINT.search(b):
        return b, a
    return a, b


def parse_resume_text(text: str) -> list[Fact]:
    facts: list[Fact] = []
    section: Section = "other"
    cur: Fact | None = None
    pending: str = ""            # a header line whose date range is on the next line
    skills: list[str] = []
    for raw in text.splitlines():
        line = raw.rstrip()
        if not line.strip():
            continue
        sec = _section_of(line)
        if sec:
            section, cur, pending = sec, None, ""
            continue
        if section == "skills":
            skills += [s.strip() for s in re.split(r"[,;|]", re.sub(r"^[^:]{1,30}:", "", line)) if s.strip()]
            continue
        m = _RANGE.search(line)
        if m and not _BULLET.match(line):
            head = (line[:m.start()] + " " + line[m.end():]).strip() or pending
            title, org = _split_head(head, section)
            end_raw = m.group("e")
            cur = Fact(section=section, org=org, title=title, start=parse_date(m.group("s")),
                       end=parse_date(end_raw, end=True), current=not parse_date(end_raw, end=True))
            facts.append(cur)
            pending = ""
            continue
        if _BULLET.match(line):
            b = _BULLET.sub("", line).strip()
            if cur is not None:
                cur.bullets.append(b)
                cur.metrics += [x.strip() for x in _METRIC.findall(b)]
            continue
        # a plain line: either the first half of a two-line header or a continuation sentence
        if cur is not None and cur.bullets:
            cur.bullets[-1] += " " + line.strip()
            cur.metrics += [x.strip() for x in _METRIC.findall(line)]
        else:
            pending = line.strip()
    if skills:
        facts.append(Fact(section="skills", title="Skills", bullets=[", ".join(dict.fromkeys(skills))]))
    return facts


def read_resume_text(path: str | Path) -> str:
    p = Path(path).expanduser()
    if p.suffix.lower() == ".pdf":
        try:
            from pypdf import PdfReader  # optional extra: pip install 'job-apply-mcp[resume]'
        except ImportError as e:
            raise RuntimeError("PDF resumes need the optional extra: pip install 'job-apply-mcp[resume]' "
                               "(or point paths.resume at a .txt/.md export)") from e
        return "\n".join((pg.extract_text() or "") for pg in PdfReader(str(p)).pages)
    return p.read_text(encoding="utf-8", errors="replace")


def load_resume(path: str | Path) -> list[Fact]:
    return parse_resume_text(read_resume_text(path))


def save_facts(facts: list[Fact], path: str | Path) -> None:
    Path(path).expanduser().write_text(json.dumps([f.model_dump() for f in facts], indent=1), encoding="utf-8")


def today_local() -> date:
    return datetime.now().astimezone().date()


def ym(d: date) -> str:
    return f"{d.year}-{d.month:02d}"


def is_past(end: str, today: date) -> bool:
    return bool(end) and end < ym(today)


def completed_education(resume: list[Fact], today: date) -> list[Fact]:
    return [f for f in resume if f.section == "education" and not f.current and is_past(f.end, today)]


def temporal_phrasing(resume: list[Fact], today: date) -> list[str]:
    """Writer-facing tense guidance derived only from resume dates."""
    out: list[str] = []
    for f in resume:
        who = ", ".join(x for x in (f.title, f.org) if x)
        if f.section == "education":
            if f.current or (f.end and not is_past(f.end, today)):
                out.append(f"TIMELINE: {who} is in progress (expected {f.end or 'TBD'}); use present tense.")
            else:
                out.append(f"TIMELINE: {who} was completed {f.end}; say 'completed' or 'graduated', never "
                           "'currently pursuing/completing'.")
        elif f.section == "experience":
            if f.current:
                out.append(f"TIMELINE: current role is {who} (since {f.start}); present tense.")
            else:
                out.append(f"TIMELINE: {who} ended {f.end}; past tense only.")
    return out

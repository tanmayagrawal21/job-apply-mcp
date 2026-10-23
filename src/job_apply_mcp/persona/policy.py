"""PersonaPolicy: the small interface the mapper, the salary rule and the writer use (ADR-0047).

    claim(label, canonical_key)  -> value | None   answer a free-form question from stored/persona answers
    stored(key)                  -> value | None   a stored answer for a canonical key (fills gaps, never overrides)
    counted_spans(roles)         -> [(start, end)] roles that count as professional experience
    salary(cfg, posting, expected) -> SalaryDecision | None
    exceptional(posting_text)    -> Project | None the most relevant project for this posting

The current posting (company, title, text) travels in a ContextVar set by the engine, so parallel plans never see
each other's posting and the mapper keeps its signature.
"""
from __future__ import annotations

import re
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass
from typing import Any, ClassVar, Protocol, runtime_checkable

from .gaps import ProfileDB
from .keys import gap_key, normalize_question, role_family
from .models import ExceptionalWork, ExperienceCounting, Persona, Project, SalaryPolicy, pick_project


@dataclass
class PostingCtx:
    company: str = ""
    title: str = ""
    text: str = ""


_CURRENT: ContextVar[PostingCtx | None] = ContextVar("persona_posting", default=None)


@contextmanager
def posting_scope(company: str, title: str, text: str = "") -> Iterator[None]:
    tok = _CURRENT.set(PostingCtx(company, title, text))
    try:
        yield
    finally:
        _CURRENT.reset(tok)


@runtime_checkable
class PersonaLike(Protocol):
    def claim(self, label: str, canonical: str | None) -> str | None: ...
    def stored(self, key: str) -> str | None: ...


@dataclass
class Role:
    start: str
    end: str            # "" = current
    title: str = ""
    org: str = ""
    kind: str = ""      # full_time | internship | part_time | academic | volunteer | freelance | "" (infer)


_KINDS: list[tuple[str, re.Pattern[str]]] = [
    ("internship", re.compile(r"\bintern(ship)?\b|\bco-?op\b", re.IGNORECASE)),
    ("part_time", re.compile(r"\bpart[- ]time\b", re.IGNORECASE)),
    ("academic", re.compile(r"\b(research|teaching|graduate|undergraduate|student) (assistant|fellow)\b|"
                            r"\bgrader\b|\btutor\b", re.IGNORECASE)),
    ("volunteer", re.compile(r"\bvolunteer\b", re.IGNORECASE)),
    ("freelance", re.compile(r"\bfreelanc\w*\b|\bself[- ]employed\b", re.IGNORECASE)),
]


def role_kind(r: Role) -> str:
    if r.kind:
        return r.kind
    for kind, pat in _KINDS:
        if pat.search(r.title):
            return kind
    return "full_time"


def _same(a: str, b: str) -> bool:
    return normalize_question(a) == normalize_question(b) and bool(normalize_question(a))


def counts(r: Role, ec: ExperienceCounting) -> bool:
    if any(_same(r.org, o) for o in ec.exclude_orgs):
        return False
    if any(_same(r.org, o) for o in ec.include_orgs):
        return True
    if ec.count_from and r.start and r.start < ec.count_from:
        return False
    k = role_kind(r)
    return ec.mode == "all" or k == "full_time" or (ec.mode == "include_internships" and k == "internship")


class PersonaPolicy:
    def __init__(self, persona: Persona | None = None, db: ProfileDB | None = None,
                 resume_projects: Callable[[], list[Project]] | None = None):
        self.persona = persona or Persona()
        self.db = db
        self._resume_projects = resume_projects

    # -- context ---------------------------------------------------------------------------------
    @staticmethod
    def current() -> PostingCtx:
        return _CURRENT.get() or PostingCtx()

    def _scoped(self, key: str) -> str | None:
        if self.db is None:
            return None
        c = self.current()
        return self.db.answer(key, company=c.company, family=role_family(c.title) if c.title else "")

    # -- effective persona = TOML overlaid with stored answers to the persona.* questions ----------
    def effective(self) -> Persona:
        p = self.persona
        if self.db is None:
            return p
        out = p.model_copy(deep=True)
        mode = self._scoped("persona.experience_counting")
        if mode:
            if mode in ExperienceCounting.model_fields["mode"].annotation.__args__:
                out.experience_counting = out.experience_counting.model_copy(update={"mode": mode})
            else:
                out.experience_counting = out.experience_counting.model_copy(update={"note": mode})
        fig = self._scoped("persona.salary_no_range")
        if fig and (m := re.search(r"\d[\d,]*", fig)):
            out.salary_policy = out.salary_policy.model_copy(update={"fixed": int(m.group(0).replace(",", ""))})
        proj = self._scoped("persona.exceptional_work")
        if proj:
            name, _, summary = proj.partition(":")
            extra = Project(name=name.strip(), summary=summary.strip())
            out.exceptional_work = out.exceptional_work.model_copy(
                update={"projects": [extra, *out.exceptional_work.projects]})
        return out

    _QKEY: ClassVar[dict[str, str]] = {"experience_counting": "persona.experience_counting", "salary_policy": "persona.salary_no_range",
             "exceptional_work": "persona.exceptional_work"}

    def configured(self, name: str) -> bool:
        """True when the owner set the policy in the TOML or answered its question."""
        return self.persona.configured(name) or self._scoped(self._QKEY[name]) is not None

    # -- answers -----------------------------------------------------------------------------------
    def stored(self, key: str) -> str | None:
        return self._scoped(key) or self.persona.answers.get(key)

    def claim(self, label: str, canonical: str | None) -> str | None:
        """Free-form (non-canonical) questions only: a stored or persona answer for the normalized question."""
        if canonical is not None:
            return None
        key = gap_key(label, None)
        if key.startswith("persona."):
            return None
        if (v := self.stored(key)) is not None:
            return v
        q = normalize_question(label)
        best: tuple[int, str] | None = None
        for pat, val in self.persona.answers.items():
            p = normalize_question(pat)
            if (p and not pat.startswith(("q:", "ai_tools")) and re.search(rf"\b{re.escape(p)}\b", q)
                    and (best is None or len(p) > best[0])):
                best = (len(p), val)
        return best[1] if best else None

    # -- experience --------------------------------------------------------------------------------
    def counted_spans(self, roles: list[Role]) -> list[tuple[str, str]]:
        ec = self.effective().experience_counting
        return [(r.start, r.end) for r in roles if r.start and counts(r, ec)]

    # -- salary ------------------------------------------------------------------------------------
    def salary(self, cfg: Any, posting: Any, expected: int | None = None):
        from ..apply.salary import SalaryDecision, _round, compute_ask
        from ..policy.gates import parse_salary
        sp: SalaryPolicy = self.effective().salary_policy
        upd: dict[str, Any] = {}
        if sp.floor is not None:
            upd.update(base_salary=sp.floor, salary_floor=sp.floor, col_multipliers={})
        fig = sp.fixed if sp.fixed is not None else (expected if expected is not None else cfg.default_ask)
        if fig is not None:
            upd["default_ask"] = fig
        c2 = cfg.model_copy(update=upd) if upd else cfg
        dec = compute_ask(c2, posting)
        rng = parse_salary(getattr(posting, "salary", "") or "", cfg.hours_per_year)
        if dec is None or not rng or sp.mode == "midpoint":
            if dec is not None:
                dec.audit["policy"] = "persona" if upd else "default"
            return dec
        lo, hi = rng
        floor = dec.audit.get("floor") or 0
        ask = _round(max(lo if sp.mode == "floor" else hi, floor), cfg.salary_round_to, hi)
        dec = SalaryDecision(ask, {**dec.audit, "rule": f"range:{sp.mode}", "ask": ask, "policy": "persona"})
        return dec

    # -- exceptional work --------------------------------------------------------------------------
    def projects(self) -> list[Project]:
        ew: ExceptionalWork = self.effective().exceptional_work
        extra: list[Project] = []
        if ew.use_resume_projects and self._resume_projects is not None:
            have = {p.name.lower() for p in ew.projects}
            extra = [p for p in self._resume_projects() if p.name.lower() not in have]
        return [*ew.projects, *extra]

    def exceptional(self, posting_text: str | None = None) -> Project | None:
        c = self.current()
        text = posting_text if posting_text is not None else f"{c.title} {c.text}"
        return pick_project(self.projects(), text)

"""Thin adapters from the concrete components (profile.py, policy/gates.py, llm/router.py) onto the engine's
Protocols in ports.py. Imports of those components are lazy so the apply package loads without them."""
from __future__ import annotations

import re
from typing import Any

from ..config import Settings
from ..core.models import ApplicationForm, GateResult, Posting
from .ports import ComponentMissing


class ProfileView:
    """Wraps profile.Profile as ProfileLike. Canonical keys map to structured fields; anything else is
    looked up as a question label in the profile's pattern answers, then in demographics."""

    def __init__(self, profile: Any, settings: Settings):
        self.p = profile
        self.resume_path = settings.paths.resume
        self.cover_letter_path: str | None = None

    def _current_work(self):
        cur = [w for w in self.p.work if not w.end]
        return (cur or self.p.work or [None])[0]

    def _edu(self):
        return max(self.p.education, key=lambda e: e.end or "") if self.p.education else None

    def lookup(self, key: str) -> Any | None:
        c, links, d = self.p.contact, self.p.links, self.p.demographics
        w, e = self._current_work(), self._edu()
        table: dict[str, Any] = {
            "first_name": c.first_name, "last_name": c.last_name, "full_name": c.full_name,
            "preferred_name": c.first_name, "email": c.email, "phone": c.phone,
            "location": ", ".join(x for x in (c.city, c.region) if x), "country": c.country,
            "zip": c.postal_code, "linkedin": links.get("linkedin"), "github": links.get("github"),
            "website": links.get("website") or links.get("portfolio"),
            "current_company": w.company if w else None, "current_title": w.title if w else None,
            "school": e.school if e else None, "degree": e.degree if e else None,
            "discipline": e.field if e else None, "graduation": e.end if e else None,
            "lat": c.lat, "lon": c.lon, "sexual_orientation": d.sexual_orientation,
            "gender": d.gender, "hispanic": d.race_ethnicity, "race": d.race_ethnicity,
            "veteran": d.veteran_status, "disability": d.disability_status, "pronouns": d.pronouns,
            "salary_expectation": (str(self.p.salary.expected) if self.p.salary.expected
                                   else self.p.salary.text),
        }
        if key in table:
            v = table[key]
            return v if isinstance(v, float) else (v or None)
        q = key.replace("_", " ")
        return self.p.answer_for(q) or self.p.demographic_for(q)

    def facts(self, topic: str = "", limit: int = 12) -> list[str]:
        rows: list[str] = []
        for w in self.p.work:
            rows.append(f"{w.title} at {w.company} ({w.start}-{w.end or 'present'}): {w.summary}".strip(": "))
        for e in self.p.education:
            rows.append(f"{e.degree} in {e.field}, {e.school} (to {e.end})".replace("  ", " "))
        if self.p.skills:
            rows.append("Skills: " + ", ".join(self.p.skills))
        words = set(re.findall(r"[a-z]{4,}", topic.lower()))
        rows.sort(key=lambda r: -len(words & set(re.findall(r"[a-z]{4,}", r.lower()))))
        return [r[:300] for r in rows[:limit]]


class PolicyGates:
    """GatesLike over policy.gates.evaluate_posting (ADR-0009)."""

    def __init__(self, settings: Settings):
        from ..policy.gates import evaluate_posting
        self._eval = evaluate_posting
        self.cfg = settings.policy

    async def evaluate(self, posting: Posting, form: ApplicationForm) -> GateResult:
        return self._eval(posting, self.cfg, " ".join(f.label for f in form.fields))


class RouterView:
    """RouterLike over llm.router.LLMRouter. 'short' maps to the router's ordinary field tiers."""

    def __init__(self, router: Any):
        self.r = router

    async def available(self, task_class: str) -> bool:
        try:
            tier = self.r.cfg.task_tiers.get(task_class)
            if tier is None:
                return True
            cands = await self.r._candidates(task_class)
            return any(c.provider != "client" for c in cands)
        except Exception:  # noqa: BLE001
            return False

    async def complete(self, prompt: str, *, task_class: str = "short", system: str | None = None,
                       max_tokens: int = 256, json_schema: dict | None = None) -> str:
        from ..llm.base import Task
        cls = "field" if task_class == "short" else task_class
        res = await self.r.run(Task(prompt=prompt, system=system, max_tokens=max_tokens, json_schema=json_schema,
                                    task_class=cls))
        if not res.ok or not res.text:
            raise ComponentMissing(res.error or "router returned no text (client must write)")
        return res.text

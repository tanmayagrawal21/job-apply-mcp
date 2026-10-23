"""Thin adapters from the concrete components (profile.py, policy/gates.py, llm/router.py) onto the engine's
Protocols in ports.py. Imports of those components are lazy so the apply package loads without them."""
from __future__ import annotations

import re
from typing import Any, ClassVar

from ..config import Settings
from ..core.models import ApplicationForm, GateResult, Posting
from .mapper import is_us_country
from .ports import ComponentMissing

_CO_SUFFIX = frozenset(["inc", "llc", "ltd", "corp", "corporation", "co", "company", "technologies", "technology",
                        "labs", "group", "holdings", "the", "usa", "us", "global", "incorporated", "limited"])


def _co_key(name: str) -> str:
    toks = [t for t in re.sub(r"[^a-z0-9]+", " ", str(name).lower()).split() if t not in _CO_SUFFIX]
    return "".join(toks)


def same_company(a: str, b: str) -> bool:
    """Alias-tolerant employer match: legal suffixes ignored, "DoorDash" == "DoorDash USA" == "Door Dash Inc"."""
    ka, kb = _co_key(a), _co_key(b)
    if not ka or not kb:
        return False
    return ka == kb or (min(len(ka), len(kb)) >= 5 and (ka.startswith(kb) or kb.startswith(ka)))


class ProfileView:
    """Wraps profile.Profile as ProfileLike. Canonical keys map to structured fields; anything else is
    looked up as a question label in the profile's pattern answers, then in demographics."""

    def __init__(self, profile: Any, settings: Settings):
        self.p = profile
        self.resume_path = settings.paths.resume
        self.cover_letter_path: str | None = None
        self.settings = settings
        self._fs: Any = None
        self._corpus: list[str] | None = None
        self.persona: Any = None      # PersonaPolicy (ADR-0047); None = behave exactly as before

    # -- resume-first facts (ADR-0023) ------------------------------------------------------------
    @property
    def fact_set(self) -> Any:
        """Precedence-resolved facts: resume (1) > profile and non-conflicting user-letter facts (2)."""
        if self._fs is None:
            from ..profile.facts import resolve_facts
            from ..profile.resume import load_resume
            from ..writing.quality import read_corpus
            resume: list[Any] = []
            if self.resume_path:
                try:
                    resume = load_resume(self.resume_path)
                except (OSError, RuntimeError):
                    resume = []
            w = self.settings.writing
            letters = read_corpus([x for x in [w.style_exemplar, *w.style_corpus] if x])
            self._fs = resolve_facts(resume, self.p, letters)
        return self._fs

    @property
    def resume_facts(self) -> list[Any]:
        return self.fact_set.resume

    @property
    def corpus(self) -> list[str]:
        """The user's own letters (exemplar + style_corpus + user-trusted cover letters in provenance)."""
        if self._corpus is None:
            from ..writing.quality import read_corpus
            w = self.settings.writing
            c = read_corpus([x for x in [w.style_exemplar, *w.style_corpus] if x])
            try:
                from pathlib import Path

                from ..provenance import ProvenanceStore
                from ..provenance.models import USER_TRUSTED
                db = self.settings.provenance.db_path or Path(self.settings.paths.data_dir).expanduser() / "provenance.db"
                if Path(db).is_file():
                    c += [r.text for r in ProvenanceStore(db).list_by_origin(USER_TRUSTED, ["cover_letter"])][:8]
            except Exception:  # noqa: BLE001, S110
                pass
            self._corpus = c[:8]
        return self._corpus

    def _current_work(self):
        cur = [w for w in self.p.work if not w.end]
        return (cur or self.p.work or [None])[0]

    def _work_fact(self, key: str) -> str | None:
        """ADR-0053: "current_*" = the one [[work]] entry with no end date; "recent_*" = that entry, else the entry
        with the latest end date. Several current entries, or a tie, is ambiguous (None)."""
        cur = [w for w in self.p.work if not w.end]
        if key.startswith("recent_") and not cur:
            ended = sorted((w for w in self.p.work if w.end), key=lambda w: w.end, reverse=True)
            cur = ended[:1] if len(ended) == 1 or (ended and ended[0].end != ended[1].end) else []
        if len(cur) != 1:
            return None
        w = cur[0]
        return (w.company if key.endswith("_company") else w.title).strip() or None

    def _edu(self):
        return max(self.p.education, key=lambda e: e.end or "") if self.p.education else None

    def _edu_fact(self, key: str) -> str | None:
        """ADR-0055: education facts from [[education]] only. "recent_*" = the entry with the latest end date (a tie or an
        undated entry among several is ambiguous); "recent_degree_completed" = the latest entry whose end date has
        passed; "highest_degree" = the entry with the highest degree level (an unrecognised level is ambiguous)."""
        from datetime import UTC, datetime

        from .mapper import degree_level
        rows = [e for e in self.p.education if e.school.strip() or e.degree.strip()]
        if key == "recent_degree_completed":
            today = datetime.now(UTC).strftime("%Y-%m")
            rows = [e for e in rows if re.fullmatch(r"\d{4}-\d{2}", e.end or "") and e.end <= today]
        if not rows:
            return None
        if key == "highest_degree":
            rank = {"High School": 0, "Associate": 1, "Bachelor": 2, "Master": 3, "M.B.A": 3, "Doctor": 4}
            lv = [degree_level(e.degree) for e in rows]
            if any(x is None for x in lv):
                return None
            top = max(rank[x] for x in lv if x)
            best = {x for x in lv if x and rank[x] == top}
            if len(best) != 1:
                return None
            e = max((e for e, x in zip(rows, lv, strict=True) if x in best), key=lambda e: e.end or "")
            return e.degree.strip() or None
        if len(rows) > 1 and any(not e.end for e in rows):
            return None
        ends = sorted((e.end for e in rows), reverse=True)
        if len(ends) > 1 and ends[0] == ends[1]:
            return None
        e = max(rows, key=lambda e: e.end or "")
        return (e.school if key == "recent_school" else e.degree).strip() or None

    def _last_internship(self) -> str | None:
        """ADR-0055: the company of the [[work]] entry marked kind="internship" (or titled "Intern") that is current or
        ended last. A tie is ambiguous."""
        rows = [w for w in self.p.work if w.kind == "internship"
                or (not w.kind and re.search(r"\bintern(?:ship)?\b", w.title, re.IGNORECASE))]
        cur = [w for w in rows if not w.end]
        if cur:
            return (cur[0].company.strip() or None) if len(cur) == 1 else None
        ends = sorted((w.end for w in rows), reverse=True)
        if not ends or (len(ends) > 1 and ends[0] == ends[1]):
            return None
        return max(rows, key=lambda w: w.end).company.strip() or None

    def _us_person(self) -> bool | None:
        """ADR-0055: [authorization].us_person, else policy.us_person only when it was set in the TRUSTED user config
        (load_settings marks it). A default, a project file or JOB_APPLY__* is never a fact."""
        v = self.p.authorization.us_person
        if v is None and getattr(self.settings.policy, "_us_person_trusted", False):
            v = self.settings.policy.us_person
        return v

    # canonical key -> [answers] phrases tried (the user's own pattern wins; nothing is invented)
    _ANSWER_PHRASES: ClassVar[dict[str, tuple[str, ...]]] = {
        "work_authorized": ("work authorized", "authorized to work", "eligible to work", "work authorization",
                            "authorization to work", "legally authorized"),
        "requires_sponsorship": ("requires sponsorship", "require sponsorship", "sponsorship", "visa sponsorship"),
        "willing_to_relocate": ("willing to relocate", "relocate", "relocation"),
        "relocation_support": ("relocation support", "relocation assistance"),
        "open_to_hybrid": ("open to hybrid", "hybrid"),
    }
    _TRI: ClassVar[dict[bool, str]] = {True: "Yes", False: "No"}

    def _experience_spans(self) -> list[tuple[str, str]]:
        """Resume experience entries win (ADR-0023); the profile's work list is the fallback. With a persona, only
        the roles its experience_counting policy counts (ADR-0047)."""
        res = [(f.start, "" if f.current else f.end) for f in self.resume_facts
               if f.section == "experience" and f.start]
        if self.persona is None:
            return res or [(w.start, w.end) for w in self.p.work if w.start]
        from ..persona import Role
        kinds = {(w.company.lower(), w.start): w.kind for w in self.p.work}
        roles = [Role(f.start, "" if f.current else f.end, f.title, f.org, kinds.get((f.org.lower(), f.start), ""))
                 for f in self.resume_facts if f.section == "experience" and f.start]
        roles = roles or [Role(w.start, w.end, w.title, w.company, w.kind) for w in self.p.work if w.start]
        return self.persona.counted_spans(roles)

    def resume_projects(self) -> list[Any]:
        from ..persona import Project
        return [Project(name=f.title or f.org, summary="; ".join(f.bullets[:2]))
                for f in self.resume_facts if f.section == "projects" and (f.title or f.org)]

    def _structured(self, key: str) -> bool | None:
        a, pr = self.p.authorization, self.p.preferences
        home = a.facts_for(a.home_country) if a.home_country else None   # ADR-0050: flat keys need a home country
        return {"work_authorized": home and home.work_authorized,
                "requires_sponsorship": home and home.requires_sponsorship,
                "requires_sponsorship_now": home and home.requires_sponsorship_now,
                # ADR-0040: US citizenship comes from the US facts (flat `citizen` only when home_country is US)
                "us_citizen": a.facts_for("US").citizen, "willing_to_relocate": pr.willing_to_relocate,
                "relocation_support": pr.needs_relocation_support, "open_to_hybrid": pr.open_to_hybrid,
                "open_to_onsite": pr.open_to_onsite,
                "acknowledge_privacy_notices": pr.acknowledge_privacy_notices}.get(key)

    def _citizenship_country(self) -> str | None:
        """[authorization].citizenship, else the user's own "country of citizenship" answer (never a Yes/No)."""
        c = (self.p.authorization.citizenship or self.p.answer_for("country of citizenship") or "").strip()
        return None if not c or c.lower() in ("yes", "no") else c

    def _gpa(self, level: str) -> str | None:
        """Education `gpa` for the degree level asked ('undergrad' | 'graduate' | 'doctorate'); 'any' only when
        exactly one education row has a gpa. Ambiguous or missing -> None (never guessed)."""
        from .mapper import degree_level
        want = {"undergrad": "Bachelor", "graduate": "Master", "doctorate": "Doctor"}.get(level)
        rows = [e for e in self.p.education if (e.gpa or "").strip()]
        rows = [e for e in rows if degree_level(e.degree) == want] if want else rows
        vals = {e.gpa.strip() for e in rows}
        return vals.pop() if len(vals) == 1 else None

    def _employers(self) -> list[str]:
        orgs = [w.company for w in self.p.work]
        return orgs + [f.org for f in self.resume_facts if f.section == "experience" and f.org]

    def _derived(self, key: str) -> Any | None:
        if key == "work:several_current":           # ADR-0057
            return "Yes" if sum(1 for w in self.p.work if not w.end) > 1 else None
        if key == "work:last_internship":           # ADR-0055
            return self._last_internship()
        if key.startswith("work:"):                 # ADR-0053: employer / title facts from [[work]]
            return self._work_fact(key[5:])
        if key.startswith("edu:"):                  # ADR-0055: [[education]] facts
            return self._edu_fact(key[4:])
        if key == "us_person":                      # ADR-0055: structured fact only, never an [answers] pattern
            v = self._us_person()
            return None if v is None else self._TRI[v]
        if key == "work_history":
            return "Yes" if self._employers() else None
        if key in ("contact:city", "contact:region"):
            return getattr(self.p.contact, key[8:]).strip() or None
        if key == "derived:region":                 # ADR-0040: state/region for the continental-US rule
            return self.p.contact.region or None
        if key == "home_country":
            return self.p.authorization.home_country.upper() or None   # ADR-0050: no US default
        if key.startswith("auth:"):                 # ADR-0040: "auth:<ISO-2>:<fact>" per-country authorization facts
            _, cc, fact = key.split(":", 2)
            a = self.p.authorization
            facts = a.facts_for(cc)
            v = getattr(facts, fact, None)
            if v is None and fact == "citizen" and (c := self._citizenship_country()):
                from .places import country_iso
                iso = country_iso(c)
                v = (iso == cc.upper()) if iso else None
            if v is None and self.persona is not None:   # ADR-0047: the owner's answer to the recorded per-country gap
                return self.persona.stored(f"authorization.{cc.upper()}.{fact}")   # an explicit profile fact wins
            return None if v is None else self._TRI[v]
        if key == "citizenship_country":
            return self._citizenship_country()
        if key.startswith("gpa:"):
            return self._gpa(key[4:])
        if key.startswith("country_answer:"):       # ADR-0038: only an [answers] pattern that names the country
            from .mapper import question_countries
            _, country, question = key.split(":", 2)
            mine = {pat: v for pat, v in self.p.answers.items() if country in question_countries(pat)[1]}
            return self.p.model_copy(update={"answers": mine}).answer_for(question) if mine else None
        if key.startswith("employer:"):
            want = key[9:]
            return "Yes" if any(same_company(o, want) for o in self._employers()) else None
        if key == "us_citizen" and self._structured(key) is None and (c := self._citizenship_country()):
            return self._TRI[is_us_country(c)]
        if key == "years_experience":
            from .derive import total_years
            return total_years(self._experience_spans())
        if key == "skill_years":
            return {k.strip().lower(): float(v) for k, v in self.p.skill_years.items()} or None
        if key == "languages":
            return list(self.p.languages) or None
        if key.startswith("referral:"):
            want = re.sub(r"[^a-z0-9]+", " ", key[9:].lower()).strip()
            for co, who in self.p.referrals.items():
                n = re.sub(r"[^a-z0-9]+", " ", co.lower()).strip()
                if n and want and (n == want or re.search(rf"\b{re.escape(n)}\b", want)):
                    return who or "Yes"
            return None
        if key in self._ANSWER_PHRASES or key in ("us_citizen", "open_to_onsite",
                                                       "acknowledge_privacy_notices"):
            st = self._structured(key)
            if st is not None:
                return self._TRI[st]
            if key in ("work_authorized", "requires_sponsorship"):
                return None                        # ADR-0050: never a flat [answers] pattern
            for q in self._ANSWER_PHRASES.get(key, ()):
                if (v := self.p.answer_for(q)) is not None:
                    return v
        return None

    def lookup(self, key: str) -> Any | None:
        if key.startswith("structured:"):   # ADR-0050: the [authorization]/[preferences] value only, never [answers]
            st = self._structured(key[11:])
            return None if st is None else self._TRI[st]
        if key.startswith("optin:"):      # ADR-0050: only the owner's exact [answers] key, never a stored/persona answer
            want = key.lower()
            return next((v for k, v in self.p.answers.items() if k.strip().lower() == want), None)
        v = self._lookup(key)
        if (self.persona is not None and (v is None or v == "") and " " not in key
                and not key.startswith(("work:", "contact:", "edu:")) and key not in ("work_history", "us_person")):
            s = self.persona.stored(key)     # stored answers fill gaps and defaults; they never override the profile
            if s is not None:
                return s
        return v

    def _lookup(self, key: str) -> Any | None:
        if (key in ("years_experience", "skill_years", "languages", "us_citizen", "open_to_onsite", "relocation_support",
                    "open_to_hybrid", "citizenship_country", "acknowledge_privacy_notices", "us_person")
                or key.startswith(("referral:", "gpa:", "employer:", "country_answer:", "derived:", "auth:", "work:",
                                   "contact:", "edu:"))
                or key in ("home_country", "work_history")):
            return self._derived(key)
        if key in self._ANSWER_PHRASES and (v := self._derived(key)) is not None:
            return v
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
            "gender": d.gender, "gender_if_required": d.gender_if_required, "hispanic": d.hispanic_latino,
            "race": d.race_ethnicity, "race_detail": d.race_detail,
            "veteran": d.veteran_status, "disability": d.disability_status, "pronouns": d.pronouns,
            "salary_expectation": (str(self.p.salary.expected) if self.p.salary.expected
                                   else self.p.salary.text),
        }
        if key == "located_in_us" and c.country:       # derived from the configured country; unknown -> unmapped
            table[key] = "Yes" if re.sub(r"[^a-z]", "", c.country.lower()) in ("us", "usa", "unitedstates",
                                                                                "unitedstatesofamerica") else "No"
        if key in table:
            v = table[key]
            return v if isinstance(v, float) else (v or None)
        q = key.replace("_", " ")
        return self.p.answer_for(q) or self.p.demographic_for(q)

    def facts(self, topic: str = "", limit: int = 12) -> list[str]:
        """Only the precedence-resolved fact set (ADR-0023): never raw letters or unresolved profile text."""
        rows = list(self.fact_set.lines)
        lead: list[str] = []
        if self.persona is not None:
            from ..persona.keys import is_exceptional_label
            if is_exceptional_label(topic) and (proj := self.persona.exceptional()) is not None:
                lead = [f"EXCEPTIONAL WORK (use this project): {proj.name}" + (f": {proj.summary}" if proj.summary else "")][:1]
        words = set(re.findall(r"[a-z]{4,}", topic.lower()))
        rows.sort(key=lambda r: (r.startswith("TIMELINE"), -len(words & set(re.findall(r"[a-z]{4,}", r.lower())))))
        keep = [r for r in rows if not r.startswith("TIMELINE")][:limit]
        return [r[:300] for r in lead + keep][:max(limit, len(lead))]


class PolicyGates:
    """GatesLike over policy.gates.evaluate_posting (ADR-0009)."""

    def __init__(self, settings: Settings):
        from ..policy.gates import evaluate_posting
        self._eval = evaluate_posting
        self.cfg = settings.policy

    async def evaluate(self, posting: Posting, form: ApplicationForm) -> GateResult:
        # one line per field (label and its options) so a phrase never spans two fields (ADR-0040)
        lines = [" | ".join([f.label, *f.options]).replace("\n", ". ") for f in form.fields]
        return self._eval(posting, self.cfg, "\n".join(lines))


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

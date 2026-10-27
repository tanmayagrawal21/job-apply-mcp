"""The mapper's view of the candidate journal (ADR-0064).

`map_fields(..., journal=view)` wraps the profile in a `ProfileOverlay`: every lookup asks the profile FIRST; the
journal answers only a key the profile leaves empty, and only with an entry that passes the use-time gate
(`Entry.usable_for_forms`). All of the mapper's own guards (EEO isolation, per-country authorization, residence
coupling, option checks) still run on the value, because the journal only ever answers through `lookup`.

Provenance: an answer that used a journal entry gets `source = "journal:<entry id>@<date>"`.
Conflicts: when the profile and a usable journal entry disagree, the field is left unresolved with a
`conflict` hint for the owner. Neither value is picked.
"""
from __future__ import annotations

import contextlib
import re
from typing import Any

from .store import CandidateJournal, Entry

_DEMOGRAPHIC = ("gender", "hispanic", "race", "veteran", "disability", "pronouns", "sexual_orientation")
_AUTH_FACTS = ("work_authorized", "requires_sponsorship", "requires_sponsorship_now", "citizen")
# journal key -> the profile lookup keys it may answer. Anything not listed is recorded but never fills a form.
_STATIC: dict[str, tuple[str, ...]] = {
    "contact.city": ("contact:city",),
    "contact.region": ("contact:region", "derived:region"),
    "contact.country": ("country",),
    "availability.start_date": ("start_date",),
    "salary.expected": ("salary_expectation",),
    "preferences.willing_to_relocate": ("structured:willing_to_relocate", "willing_to_relocate"),
    "preferences.relocation_support": ("structured:relocation_support", "relocation_support"),
    "preferences.open_to_hybrid": ("structured:open_to_hybrid", "open_to_hybrid"),
    "preferences.open_to_onsite": ("structured:open_to_onsite", "open_to_onsite"),
    "employment.current_company": ("work:current_company", "current_company"),
    "employment.current_title": ("work:current_title", "current_title"),
    "authorization.home_country": ("home_country",),
    "authorization.citizenship": ("citizenship_country",),
    "referral.source": ("referral_source",),
    **{f"demographics.{k}": (k,) for k in _DEMOGRAPHIC},
}
_BOOL_KEYS = {k for k, v in _STATIC.items() if k.startswith("preferences.")}
_YES = {"yes", "y", "true", "1"}
_NO = {"no", "n", "false", "0"}
# legal certifications are never filled from the journal, whatever key answered them
_ATTEST_LABEL = re.compile(r"certif|attest|under penalty|i agree|i acknowledge|i declare|true and (?:correct|accurate)|"
                          r"truthful", re.IGNORECASE)


def lookup_keys(key: str) -> tuple[str, ...]:
    """The profile lookup keys a journal key may answer (empty: stored for context only)."""
    if key in _STATIC:
        return _STATIC[key]
    parts = key.split(".")
    if len(parts) == 3 and parts[0] == "authorization" and re.fullmatch(r"[A-Z]{2}", parts[1]) \
            and parts[2] in _AUTH_FACTS:
        return (f"auth:{parts[1]}:{parts[2]}",)
    return ()


def _is_bool_key(key: str) -> bool:
    return key in _BOOL_KEYS or (key.startswith("authorization.") and key.rsplit(".", 1)[-1] in _AUTH_FACTS)


def form_value(e: Entry) -> str | None:
    """The entry's value in the profile's own vocabulary ("Yes"/"No" for flags, ISO-2 for home country)."""
    v = e.value.strip()
    if not v:
        return None
    if _is_bool_key(e.key):
        low = v.lower()
        return "Yes" if low in _YES else "No" if low in _NO else None
    if e.key == "authorization.home_country":
        if re.fullmatch(r"[A-Za-z]{2}", v):
            return v.upper()
        with contextlib.suppress(Exception):
            from ..apply.places import country_iso
            return country_iso(v)
        return None
    return v


def _same(a: Any, b: Any) -> bool:
    def n(x: Any) -> str:
        s = re.sub(r"[^a-z0-9]+", " ", str(x).lower()).strip()
        return "yes" if s in _YES else "no" if s in _NO else s
    return n(a) == n(b)


class JournalView:
    """The usable journal entries for one posting, indexed by profile lookup key (best scope, then newest)."""

    def __init__(self, entries: list[Entry], company: str = "", title: str = "", job_country: str | None = None):
        from ..store.sqlite import company_key
        ck, tl = company_key(company) if company else "", title.lower()
        rank: dict[str, tuple[int, str, Entry]] = {}
        for e in entries:
            if not e.usable_for_forms():
                continue
            s = e.scope
            if s == "global":
                r = 0
            elif s.startswith("country:"):
                if not job_country or s[8:] != job_country.upper():
                    continue
                r = 1
            elif s.startswith("role:"):
                if not tl or s[5:].lower() not in tl:
                    continue
                r = 2
            elif s.startswith("company:"):
                if not ck or company_key(s[8:]) != ck:
                    continue
                r = 3
            else:
                continue
            v = form_value(e)
            if v is None:
                continue
            for lk in lookup_keys(e.key):
                cur = rank.get(lk)
                if cur is None or (r, e.ts) >= cur[:2]:      # file order breaks ties: later wins
                    rank[lk] = (r, e.ts, e)
        self.index: dict[str, Entry] = {k: x[2] for k, x in rank.items()}

    def overlay(self, profile: Any) -> ProfileOverlay:
        return ProfileOverlay(profile, self)


class ProfileOverlay:
    """ProfileLike: the profile first, then the journal for keys the profile leaves empty."""

    def __init__(self, inner: Any, view: JournalView):
        object.__setattr__(self, "_inner", inner)
        object.__setattr__(self, "_view", view)
        object.__setattr__(self, "_cur", "")
        object.__setattr__(self, "served", {})       # field key -> [Entry]
        object.__setattr__(self, "conflicts", {})    # field key -> [{lookup, profile, journal, entry, date}]

    def __getattr__(self, name: str) -> Any:
        return getattr(self._inner, name)

    def __setattr__(self, name: str, value: Any) -> None:
        setattr(self._inner, name, value)

    def begin(self, field_key: str) -> None:
        object.__setattr__(self, "_cur", field_key)

    def facts(self, topic: str = "", limit: int = 12) -> list[str]:
        return self._inner.facts(topic, limit)

    def lookup(self, key: str) -> Any | None:
        pv = self._inner.lookup(key)
        e = self._view.index.get(key)
        if e is None:
            return pv
        jv = form_value(e)
        if pv is None or pv == "":
            self.served.setdefault(self._cur, []).append(e)
            return jv
        if not _same(pv, jv):
            self.conflicts.setdefault(self._cur, []).append(
                {"lookup": key, "profile": str(pv)[:120], "journal": str(jv)[:120], "entry": e.id, "date": e.date})
        return pv

    def finish(self, res: Any, fields: list[Any]) -> None:
        """Annotate journal answers with provenance; unresolve conflicts and attestation-worded fields."""
        by_key = {f.key: f for f in fields}
        keep = []
        for a in res.answers:
            f = by_key.get(a.key)
            if a.key in self.conflicts and f is not None:
                res.unresolved.append(f)
                continue
            used = self.served.get(a.key)
            if used and f is not None and _ATTEST_LABEL.search(f.label):
                res.unresolved.append(f)
                continue
            if used:
                ids = list(dict.fromkeys(f"{e.id}@{e.date}" for e in used))
                a.source = "journal:" + "+".join(ids)
            keep.append(a)
        res.answers = keep
        for k, rows in self.conflicts.items():
            res.hints.setdefault(k, {})["conflict"] = {
                "note": "your profile and the candidate journal disagree; tell the owner both values and ask "
                        "which is right (then fix profile.toml or record_candidate_fact a correction)",
                "values": rows[:3]}


def journal_view(journal: CandidateJournal | None, company: str = "", title: str = "",
                 job_country: str | None = None) -> JournalView | None:
    """The view for one posting, or None (no journal, empty, or unreadable: planning never breaks on it)."""
    if journal is None:
        return None
    try:
        entries = journal.entries()
        return JournalView(entries, company, title, job_country) if entries else None
    except Exception:  # noqa: BLE001 - the journal is advisory; a broken file must not stop planning
        return None


def journal_stories(journal: CandidateJournal | None, topic: str, company: str = "", limit: int = 3) -> list[dict]:
    if journal is None:
        return []
    try:
        return journal.stories(topic, company, limit)
    except Exception:  # noqa: BLE001
        return []

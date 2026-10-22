"""Parse "application received" emails into normalized confirmation records (ADR-0016).

Pure functions over a `Message` (sender, subject, date, short snippet). Nothing here touches the network,
the disk (except loading pattern files) or the store. Patterns live in TOML so a new ATS or employer
template is a config edit, not a code change.
"""
from __future__ import annotations

import hashlib
import re
import tomllib
from dataclasses import dataclass, field
from datetime import UTC, datetime
from email.utils import parseaddr
from importlib import resources
from pathlib import Path
from typing import Any

SNIPPET_MAX = 1200


@dataclass(slots=True)
class Message:
    """The minimum needed for parsing. The snippet is transient: it is never stored or returned."""

    sender: str
    subject: str
    date: datetime | None = None
    snippet: str = ""

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> Message:
        raw = d.get("date")
        return cls(sender=str(d.get("sender") or d.get("from") or ""), subject=str(d.get("subject") or ""),
                   date=_parse_date(raw), snippet=str(d.get("snippet") or d.get("body") or "")[:SNIPPET_MAX])


@dataclass(slots=True)
class Confirmation:
    company: str
    title: str
    platform: str
    date: datetime
    confidence: float
    sender_domain: str
    ref: str  # stable idempotency key: hash of sender + subject + day

    def row(self) -> dict[str, Any]:
        return {"co": self.company, "title": self.title, "platform": self.platform,
                "date": self.date.date().isoformat(), "conf": round(self.confidence, 2)}


@dataclass(slots=True)
class _Platform:
    name: str
    domains: tuple[str, ...]
    subject_positive: list[re.Pattern]
    extract: list[re.Pattern]
    company_from_sender: bool


@dataclass(slots=True)
class PatternSet:
    subject_positive: list[re.Pattern] = field(default_factory=list)
    subject_negative: list[re.Pattern] = field(default_factory=list)
    body_positive: list[re.Pattern] = field(default_factory=list)
    strip_words: list[re.Pattern] = field(default_factory=list)
    extract: list[re.Pattern] = field(default_factory=list)
    platforms: dict[str, _Platform] = field(default_factory=dict)


def _rx(items: list[str]) -> list[re.Pattern]:
    return [re.compile(p, re.IGNORECASE) for p in items]


def _merge(ps: PatternSet, data: dict[str, Any]) -> None:
    d = data.get("defaults", {})
    ps.subject_positive += _rx(d.get("subject_positive", []))
    ps.subject_negative += _rx(d.get("subject_negative", []))
    ps.body_positive += _rx(d.get("body_positive", []))
    ps.strip_words += [re.compile(rf"\b{w}\b", re.IGNORECASE) for w in d.get("company_strip_words", [])]
    ps.extract += _rx([e["regex"] for e in d.get("extract", [])])
    for p in data.get("platform", []):
        cur = ps.platforms.get(p["name"])
        if cur is None:
            cur = ps.platforms[p["name"]] = _Platform(p["name"], (), [], [], p.get("company_from_sender", True))
        cur.domains = tuple(dict.fromkeys(cur.domains + tuple(x.lower() for x in p.get("sender_domains", []))))
        cur.subject_positive += _rx(p.get("subject_positive", []))
        cur.extract += _rx([e["regex"] for e in p.get("extract", [])])
        if "company_from_sender" in p:
            cur.company_from_sender = p["company_from_sender"]


def load_patterns(extra_file: str | Path | None = None) -> PatternSet:
    """Built-in patterns, then the optional user file layered on top (lists append, platforms merge by name)."""
    ps = PatternSet()
    _merge(ps, tomllib.loads(resources.files(__package__).joinpath("patterns.toml").read_text("utf-8")))
    if extra_file:
        p = Path(extra_file).expanduser()
        if p.is_file():
            _merge(ps, tomllib.loads(p.read_text("utf-8")))
        else:
            raise FileNotFoundError(f"ingest.patterns_file not found: {p}")
    return ps


def _parse_date(raw: Any) -> datetime | None:
    if isinstance(raw, datetime):
        dt = raw
    elif isinstance(raw, str) and raw.strip():
        s = raw.strip()
        try:
            dt = datetime.fromisoformat(s)
        except ValueError:
            from email.utils import parsedate_to_datetime

            try:
                dt = parsedate_to_datetime(s)
            except (TypeError, ValueError):
                return None
    else:
        return None
    return dt.replace(tzinfo=UTC) if dt.tzinfo is None else dt.astimezone(UTC)


def _clean(s: str) -> str:
    s = re.sub(r"\s+", " ", s).strip(" \t-–—:|,.!\"'")
    return s


def _domain(sender: str) -> str:
    addr = parseaddr(sender)[1].lower()
    return addr.rsplit("@", 1)[-1] if "@" in addr else ""


def _domain_matches(dom: str, domains: tuple[str, ...]) -> bool:
    return any(dom == d or dom.endswith("." + d) for d in domains)


def message_ref(msg: Message, day: str) -> str:
    h = hashlib.sha256(f"{parseaddr(msg.sender)[1].lower()}\n{msg.subject.strip().lower()}\n{day}".encode())
    return "mail:" + h.hexdigest()[:20]


class ConfirmationParser:
    def __init__(self, patterns: PatternSet | None = None):
        self.p = patterns or load_patterns()

    def _company_from_sender(self, sender: str) -> str:
        name = parseaddr(sender)[0]
        for rx in self.p.strip_words:
            name = rx.sub(" ", name)
        return _clean(name)

    def _extract(self, rules: list[re.Pattern], text: str, out: dict[str, str]) -> None:
        for rx in rules:
            if out.get("company") and out.get("role"):
                return
            m = rx.search(text)
            if not m:
                continue
            for k in ("company", "role"):
                v = m.groupdict().get(k)
                if v and not out.get(k):
                    v = _clean(v)
                    if 2 <= len(v) <= 90:
                        out[k] = v

    def parse(self, msg: Message) -> Confirmation | None:
        """Return a Confirmation, or None when the message is not a recognizable application confirmation.
        Company or title may be empty strings only in `parse_loose`; this method requires both."""
        c = self.parse_loose(msg)
        return c if c and c.company and c.title else None

    def parse_loose(self, msg: Message) -> Confirmation | None:
        subj = re.sub(r"^\s*((re|fwd?)\s*:\s*)+", "", msg.subject, flags=re.IGNORECASE).strip()
        body = re.sub(r"\s+", " ", msg.snippet or "")[:SNIPPET_MAX]
        if any(rx.search(subj) for rx in self.p.subject_negative):
            return None
        dom = _domain(msg.sender)
        plat = next((pl for pl in self.p.platforms.values() if _domain_matches(dom, pl.domains)), None)
        subj_hit = any(rx.search(subj) for rx in self.p.subject_positive) or (
            plat is not None and any(rx.search(subj) for rx in plat.subject_positive))
        body_hit = any(rx.search(body) for rx in self.p.body_positive)
        if not (subj_hit or body_hit):
            return None
        found: dict[str, str] = {}
        rules = (plat.extract if plat else []) + self.p.extract
        self._extract(rules, subj, found)
        self._extract(rules, body, found)
        sender_co = ""
        if not found.get("company") and (plat is None or plat.company_from_sender):
            sender_co = self._company_from_sender(msg.sender)
            if sender_co:
                found["company"] = sender_co
        conf = 0.0
        conf += 0.3 if plat else 0.0
        conf += 0.3 if subj_hit else 0.0
        conf += 0.1 if body_hit else 0.0
        conf += 0.15 if found.get("company") and not sender_co else (0.08 if sender_co else 0.0)
        conf += 0.15 if found.get("role") else 0.0
        date = msg.date or datetime.now(UTC)
        return Confirmation(company=found.get("company", ""), title=found.get("role", ""),
                            platform=plat.name if plat else "employer", date=date, confidence=min(conf, 1.0),
                            sender_domain=dom, ref=message_ref(msg, msg.date.date().isoformat() if msg.date else "undated"))

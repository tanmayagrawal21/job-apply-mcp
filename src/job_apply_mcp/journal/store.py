"""Append-only candidate journal (ADR-0064). Stdlib only.

One JSON object per line in `<state dir>/candidate_journal.jsonl` (mode 0600). Lines are only ever appended:
a correction is a newer entry for the same key, and a retraction is a tombstone line (`op: "retract"`) naming the
entry it withdraws. Nothing is rewritten or deleted.

Capture is never blocked: anything a client sends is stored (after secret redaction), with its risk class and how
it was confirmed. Whether an entry may FILL a form is decided at use time (`usable_for_forms`, `overlay.py`).
"""
from __future__ import annotations

import json
import os
import re
import threading
import uuid
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

FILENAME = "candidate_journal.jsonl"
SOURCES = ("chat", "correction", "document")
CONFIDENCES = ("stated", "inferred", "confirmed")
KINDS = ("fact", "story")
RISKS = ("everyday", "sensitive", "attestation")
MAX_FACT, MAX_STORY, MAX_QUOTE, MAX_VALUE, MAX_CLIENT = 2000, 8000, 2000, 300, 60

# ---------------------------------------------------------------- risk classes (use-time gating)
# Keyed facts: the key's namespace decides. Text can only RAISE the class (a relocation preference that mentions a
# visa is sensitive), never lower it.
_SENSITIVE_PREFIX = ("authorization", "citizenship", "visa", "sponsorship", "immigration", "demographics", "eeo",
                     "criminal", "clearance", "background_check")
_ATTEST_PREFIX = ("attestation", "certification", "legal")
_SENSITIVE_TEXT = re.compile(
    r"\bvisas?\b|\bcitizen(?:ship)?\b|\bsponsor\w*|\bgreen card\b|\bwork permit\b|\bauthori[sz]ed to work\b|"
    r"\bwork authori[sz]ation\b|\bimmigration\b|\bh-?1b\b|\bstem opt\b|\bopt (?:ead|extension|status)\b|\bf-?1\b|"
    r"\bead\b|\bpermanent resident\b|\bgender\b|\brace\b|\bethnic\w*|\bhispanic\b|\blatin[oax]\b|\bveteran\b|"
    r"\bdisab\w*|\bpronouns?\b|\bsexual orientation\b|\btransgender\b|\bcriminal\b|\bconvict\w*|\bfelon\w*|"
    r"\bmisdemeanou?r\b|\barrest\w*|\bsecurity clearance\b|\bclearance\b|\bbackground check\b", re.IGNORECASE)
_ATTEST_TEXT = re.compile(r"\bcertif(?:y|ies|ied)\b|\battest\w*|\bunder penalty\b|\bi (?:hereby )?declare\b|"
                          r"\btrue,? (?:and )?(?:correct|accurate|complete)\b", re.IGNORECASE)


def risk_class(key: str, text: str = "") -> str:
    """'attestation' (never auto-filled), 'sensitive' (needs confirmation) or 'everyday' (usable once stated)."""
    head = key.split(".", 1)[0] if key else ""
    if head in _ATTEST_PREFIX or _ATTEST_TEXT.search(text or ""):
        return "attestation"
    if head in _SENSITIVE_PREFIX or _SENSITIVE_TEXT.search(f"{key.replace('_', ' ')} {text or ''}"):
        return "sensitive"
    return "everyday"


# ---------------------------------------------------------------- secrets: redacted at capture, never stored
_SECRET_KEY = re.compile(r"pass(?:word|code|phrase)|\bpwd\b|\botp\b|token|api_?key|secret|\bssn\b|social_security|"
                         r"card_?number|\bcvv\b|\bcvc\b|\bpin\b|credential", re.IGNORECASE)
_PASSWORD = re.compile(r"\b(?:pass(?:word|code|phrase)|pwd|pin)\b(?:\s+(?:is|was)|\s*[:=])\s*[\"']?([^\s\"']+)",
                       re.IGNORECASE)
_OTP = re.compile(r"\b(?:otp|one[- ]time (?:pass(?:word|code)|code)|verification code|security code|2fa code|"
                  r"mfa code|auth(?:entication)? code|login code|sign[- ]in code)\b[^0-9\n]{0,24}(\d{4,8})\b",
                  re.IGNORECASE)
_SSN = re.compile(r"\b\d{3}-\d{2}-\d{4}\b")
_SSN_CTX = re.compile(r"\b(?:ssn|social security(?: number)?)\b[^0-9\n]{0,16}(\d{9})\b", re.IGNORECASE)
_TOKEN = re.compile(r"\b(?:gh[pousr]_[A-Za-z0-9]{20,}|github_pat_[A-Za-z0-9_]{20,}|sk-[A-Za-z0-9_-]{16,}|"
                    r"xox[abprs]-[A-Za-z0-9-]{10,}|AKIA[0-9A-Z]{16}|AIza[0-9A-Za-z_-]{30,}|"
                    r"eyJ[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,})")
_TOKEN_CTX = re.compile(r"\b(?:api[ _-]?key|access[ _-]?key|token|secret|bearer)\b(?:\s+(?:is|was)|\s*[:=])?\s*"
                        r"[\"']?([A-Za-z0-9_\-./+=]{16,})", re.IGNORECASE)
_HIGH_ENTROPY = re.compile(r"(?<![/\w.@-])(?=[A-Za-z0-9_+=-]*\d)(?=[A-Za-z0-9_+=-]*[A-Za-z])[A-Za-z0-9_+=-]{40,}(?![\w/])")
_CARD = re.compile(r"(?<!\d)(?:\d[ -]?){12,18}\d(?!\d)")


def _luhn(digits: str) -> bool:
    total, alt = 0, False
    for ch in reversed(digits):
        d = int(ch)
        if alt:
            d = d * 2 - 9 if d > 4 else d * 2
        total += d
        alt = not alt
    return total % 10 == 0


def redact_secrets(text: str) -> tuple[str, list[str]]:
    """Replace anything that looks like a password, one-time code, token, SSN or card number with a marker.
    Returns (text, kinds found). Over-redaction is acceptable; a stored secret is not."""
    if not text:
        return text, []
    kinds: list[str] = []

    def group_sub(rx: re.Pattern[str], kind: str, s: str) -> str:
        def rep(m: re.Match[str]) -> str:
            kinds.append(kind)
            a, b = m.span(1)
            return m.group(0)[:a - m.start()] + f"[redacted:{kind}]" + m.group(0)[b - m.start():]
        return rx.sub(rep, s)

    def whole_sub(rx: re.Pattern[str], kind: str, s: str, check: Any = None) -> str:
        def rep(m: re.Match[str]) -> str:
            if check is not None and not check(m.group(0)):
                return m.group(0)
            kinds.append(kind)
            return f"[redacted:{kind}]"
        return rx.sub(rep, s)

    s = whole_sub(_TOKEN, "token", text)
    s = group_sub(_TOKEN_CTX, "token", s)
    s = group_sub(_PASSWORD, "password", s)
    s = group_sub(_OTP, "otp", s)
    s = whole_sub(_SSN, "ssn", s)
    s = group_sub(_SSN_CTX, "ssn", s)
    s = whole_sub(_CARD, "card", s, lambda t: 13 <= len(d := re.sub(r"\D", "", t)) <= 19 and _luhn(d))
    s = whole_sub(_HIGH_ENTROPY, "token", s)
    return s, sorted(set(kinds))


# ---------------------------------------------------------------- keys and scopes
_KEY_ALIASES = {"preferences.relocate": "preferences.willing_to_relocate",
                "preferences.relocation": "preferences.willing_to_relocate",
                "preferences.hybrid": "preferences.open_to_hybrid", "preferences.onsite": "preferences.open_to_onsite",
                "authorization.citizenship_country": "authorization.citizenship"}
_KEY_RX = re.compile(r"^[a-z][a-z0-9_]*(?:\.[a-z0-9_-]+)*$")
_SCOPE_RX = re.compile(r"^(global|country:[A-Z]{2}|company:.{1,120}|role:.{1,160})$")


def normalize_key(key: str) -> str | None:
    """Lower-case dotted key; the country segment of authorization.<cc>.<fact> is upper-case. None if malformed."""
    k = re.sub(r"\s+", "", (key or "").strip()).lower()
    if not k:
        return ""
    if not _KEY_RX.fullmatch(k):
        return None
    k = _KEY_ALIASES.get(k, k)
    parts = k.split(".")
    if parts[0] == "authorization" and len(parts) == 3 and len(parts[1]) == 2:
        parts[1] = parts[1].upper()
    return ".".join(parts)


def normalize_scope(scope: str) -> str | None:
    s = (scope or "global").strip()
    head, sep, rest = s.partition(":")
    head = head.strip().lower()
    if head == "global" and not rest:
        return "global"
    if sep and head == "country":
        s = f"country:{rest.strip().upper()}"
    elif sep and head in ("company", "role"):
        s = head + ":" + re.sub(r"\s+", " ", rest).strip()
    return s if _SCOPE_RX.fullmatch(s) else None


_SECRET_WORDS = frozenset(["my", "the", "a", "is", "was", "are", "its", "it", "here", "pass", "password", "passcode", "passphrase", "pwd", "pin", "code", "otp", "one", "time", "verification", "security", "token", "api", "key", "access", "secret", "bearer", "ssn", "social", "number", "card", "credit", "debit", "login", "sign", "in", "auth", "mfa", "2fa"])


def _substance(text: str) -> bool:
    """False when a text is nothing but a redacted secret and the words that label it ("password: [redacted]")."""
    rest = re.sub(r"\[redacted:\w+\]", " ", text.lower())
    return any(w not in _SECRET_WORDS for w in re.findall(r"[a-z0-9]+", rest))


def _now() -> datetime:
    return datetime.now(UTC)


@dataclass
class Entry:
    id: str
    ts: str
    date: str
    op: str = "record"                  # record | retract
    kind: str = "fact"                  # fact | story
    fact: str = ""
    key: str = ""
    value: str = ""
    scope: str = "global"
    source: str = "chat"
    quote: str = ""
    confidence: str = "stated"
    client: str = ""
    risk: str = "everyday"
    confirmed_via: str | None = None    # stated (everyday) | elicitation | client_quote | None (not confirmed)
    redacted: list[str] = field(default_factory=list)
    retracts: str = ""
    reason: str = ""
    retracted_at: str | None = None     # derived on read, never written

    def usable_for_forms(self) -> bool:
        """Use-time gate (ADR-0064): attestations never; inferred facts never; sensitive facts only once
        confirmed (elicitation, or the user's verbatim chat quote); everyday facts once stated."""
        if self.op != "record" or self.retracted_at or self.kind != "fact" or not self.key or not self.value:
            return False
        if self.risk == "attestation" or self.confidence == "inferred" or self.scope.startswith("invalid:"):
            return False
        if self.risk == "sensitive":
            return self.confirmed_via in ("elicitation", "client_quote")
        return True

    def compact(self, full: bool = False) -> dict[str, Any]:
        d: dict[str, Any] = {"id": self.id, "date": self.date, "kind": self.kind,
                             "fact": self.fact if full else self.fact[:240]}
        for k in ("key", "value"):
            if getattr(self, k):
                d[k] = getattr(self, k)
        if self.scope != "global":
            d["scope"] = self.scope
        d.update({"src": self.source, "conf": self.confidence, "risk": self.risk})
        if self.confirmed_via:
            d["confirmed_via"] = self.confirmed_via
        if self.quote:
            d["quote"] = self.quote if full else self.quote[:160]
        if self.client:
            d["client"] = self.client
        if self.redacted:
            d["redacted"] = self.redacted
        if self.retracted_at:
            d["retracted_at"] = self.retracted_at
        d["usable"] = self.usable_for_forms()
        return d


def confirmation(risk: str, source: str, quote: str, confidence: str, elicited: bool | None) -> str | None:
    """How an entry counts as confirmed. `elicited`: True/False = the user answered the confirmation dialog;
    None = the client cannot elicit (then only a verbatim chat quote stated by the user counts)."""
    if risk == "attestation" or confidence == "inferred":
        return None
    if risk == "everyday":
        return "stated"
    if elicited is True:
        return "elicitation"
    if elicited is None and source == "chat" and quote.strip() and confidence in ("stated", "confirmed"):
        return "client_quote"
    return None


class CandidateJournal:
    """Append-only JSONL journal. Safe to share across threads; every append is one O_APPEND write."""

    def __init__(self, state_dir: str | Path, filename: str = FILENAME):
        self.path = Path(state_dir).expanduser() / filename
        self._lock = threading.Lock()
        self._cache: tuple[float, int, list[Entry]] | None = None

    # ------------------------------------------------------------ write
    def _append(self, e: Entry) -> None:
        d = asdict(e)
        d.pop("retracted_at", None)
        line = json.dumps({k: v for k, v in d.items() if v not in ("", None, [])} | {"id": e.id, "op": e.op},
                          ensure_ascii=False, separators=(",", ":")) + "\n"
        with self._lock:
            self.path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
            fd = os.open(self.path, os.O_WRONLY | os.O_APPEND | os.O_CREAT, 0o600)
            try:
                os.write(fd, line.encode("utf-8"))
            finally:
                os.close(fd)
            if os.name == "posix":
                os.chmod(self.path, 0o600)
            self._cache = None

    def record(self, fact: str, *, key: str = "", value: str = "", scope: str = "global", source: str = "chat",
               quote: str = "", confidence: str = "stated", client: str = "", kind: str = "fact",
               elicited: bool | None = None) -> tuple[Entry, list[str]]:
        """Store one entry. Returns (entry, warnings). Raises ValueError only for an empty fact or a key that
        names a secret (passwords, codes, tokens, SSNs and card numbers are never journal material)."""
        warnings: list[str] = []
        kind = kind if kind in KINDS else "fact"
        if source not in SOURCES:
            warnings.append(f"source {source!r} unknown; stored as 'chat'")
            source = "chat"
        if confidence not in CONFIDENCES:
            warnings.append(f"confidence {confidence!r} unknown; stored as 'inferred'")
            confidence = "inferred"
        if key and _SECRET_KEY.search(key):
            raise ValueError("secrets (passwords, codes, tokens, SSNs, card numbers) are never recorded")
        nk = normalize_key(key)
        if nk is None:
            warnings.append(f"key {key[:60]!r} is not a dotted key like preferences.willing_to_relocate; stored "
                            "without a key")
            nk = ""
        ns = normalize_scope(scope)
        if ns is None:
            warnings.append("scope must be global, country:XX, company:<name> or role:<title>; this entry never "
                            "fills a form")
            ns = "invalid:" + (scope or "")[:60]
        redacted: list[str] = []
        cleaned: dict[str, str] = {}
        limit = MAX_STORY if kind == "story" else MAX_FACT
        for name, text, cap in (("fact", fact, limit), ("quote", quote, MAX_QUOTE), ("value", value, MAX_VALUE),
                                ("client", client, MAX_CLIENT)):
            t = (text or "").strip()
            if len(t) > cap:
                warnings.append(f"{name} truncated to {cap} chars")
                t = t[:cap]
            t, kinds = redact_secrets(t)
            redacted += kinds
            cleaned[name] = t
        if not cleaned["fact"] or not _substance(cleaned["fact"]):
            raise ValueError("fact is empty" if not cleaned["fact"] else
                             "the fact was only a secret; secrets are never recorded")
        if "[redacted:" in cleaned["value"]:
            cleaned["value"] = ""           # a structured value that contained a secret is not a value
        if kind == "story":
            cleaned["value"] = ""            # a story is essay material, never a form value
        risk = risk_class(nk, f"{cleaned['fact']} {cleaned['value']}")
        now = _now()
        e = Entry(id="cj_" + uuid.uuid4().hex[:12], ts=now.isoformat(timespec="seconds"),
                  date=now.date().isoformat(), kind=kind, fact=cleaned["fact"], key=nk, value=cleaned["value"],
                  scope=ns, source=source, quote=cleaned["quote"], confidence=confidence, client=cleaned["client"],
                  risk=risk, confirmed_via=confirmation(risk, source, cleaned["quote"], confidence, elicited),
                  redacted=sorted(set(redacted)))
        self._append(e)
        return e, warnings

    def retract(self, entry_id: str, reason: str = "", client: str = "") -> Entry:
        cur = {e.id: e for e in self.entries(include_retracted=True)}
        target = cur.get(entry_id)
        if target is None:
            raise ValueError(f"no journal entry {entry_id!r}")
        if target.retracted_at:
            raise ValueError(f"{entry_id} is already retracted")
        now = _now()
        reason, _ = redact_secrets((reason or "").strip()[:500])
        client, _ = redact_secrets((client or "").strip()[:MAX_CLIENT])
        t = Entry(id="cj_" + uuid.uuid4().hex[:12], ts=now.isoformat(timespec="seconds"), date=now.date().isoformat(),
                  op="retract", retracts=entry_id, reason=reason, client=client, fact="", risk=target.risk)
        self._append(t)
        return t

    # ------------------------------------------------------------ read
    def _load(self) -> list[Entry]:
        try:
            st = self.path.stat()
        except FileNotFoundError:
            return []
        with self._lock:
            if self._cache and self._cache[:2] == (st.st_mtime, st.st_size):
                return self._cache[2]
        rows: list[Entry] = []
        known = set(Entry.__dataclass_fields__)
        with self.path.open(encoding="utf-8") as fh:
            for line in fh:
                try:
                    d = json.loads(line)
                    rows.append(Entry(**{k: v for k, v in d.items() if k in known and k != "retracted_at"}))
                except (ValueError, TypeError):
                    continue                   # a torn or foreign line never breaks reading the rest
        with self._lock:
            self._cache = (st.st_mtime, st.st_size, rows)
        return rows

    def entries(self, include_retracted: bool = False) -> list[Entry]:
        """Recorded entries, oldest first, with `retracted_at` set from any tombstone."""
        rows = self._load()
        tomb = {e.retracts: e.ts for e in rows if e.op == "retract" and e.retracts}
        out = []
        for e in rows:
            if e.op != "record":
                continue
            if e.id in tomb:
                if not include_retracted:
                    continue
                e = Entry(**{**asdict(e), "retracted_at": tomb[e.id]})
            out.append(e)
        return out

    def stories(self, topic: str = "", company: str = "", limit: int = 3) -> list[dict[str, Any]]:
        """Story entries (essay material), most relevant to `topic` first; company-scoped stories only for that
        company. Stories never fill a form field; they are context for a person or model writing prose."""
        from ..store.sqlite import company_key
        words = set(re.findall(r"[a-z]{4,}", topic.lower()))
        ck = company_key(company) if company else ""
        rows = []
        for e in self.entries():
            if e.kind != "story":
                continue
            if e.scope.startswith("company:") and company_key(e.scope[8:]) != ck:
                continue
            if e.scope.startswith("invalid:"):
                continue
            score = len(words & set(re.findall(r"[a-z]{4,}", e.fact.lower())))
            rows.append((score, e.ts, e))
        rows.sort(key=lambda r: (r[0], r[1]), reverse=True)
        return [{"id": e.id, "date": e.date, "text": e.fact[:1200]} for _, _, e in rows[:max(0, limit)]]

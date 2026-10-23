"""Job links the user emailed (ADR-0045): URL extraction and read-only mail sources.

Bodies are parsed in memory to pull URLs out and then dropped; only the message key, sender, recipients, subject,
date and the extracted URLs leave this module. No source can change the mailbox: mbox/eml files are opened for
reading, IMAP uses EXAMINE (select readonly) + SEARCH + BODY.PEEK partial FETCH only, and client mode is plain data.
"""
from __future__ import annotations

import contextlib
import email
import hashlib
import html
import imaplib
import os
import re
from collections.abc import Callable, Iterable, Iterator
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from email import policy
from email.message import EmailMessage
from email.utils import getaddresses, parseaddr
from pathlib import Path
from typing import Any
from urllib.parse import parse_qsl, unquote, urlencode, urlsplit, urlunsplit

from ..config import ImapCfg, IntakeMailCfg
from ..ingest.confirmations import _parse_date
from ..ingest.sources import MboxSource, _credentials

_URL_RE = re.compile(r"https?://[^\s<>\"'`{}|\\^\[\]]+", re.IGNORECASE)
_HREF_RE = re.compile(r"""href\s*=\s*["']([^"']+)["']""", re.IGNORECASE)
_TRAIL = ".,;:!?)>]}'\"*"
_REDIRECT_KEYS = ("q", "url", "u", "target", "redirect", "redirect_url", "dest", "destination", "link")
_TRACKING = re.compile(r"^(utm_[a-z]+|fbclid|gclid|mc_cid|mc_eid|_hsenc|_hsmi|mkt_tok|trk|trkid|refid|ref_src)$",
                       re.IGNORECASE)


@dataclass(slots=True)
class LinkMail:
    """One message reduced to what the intake needs. The body is never kept."""
    key: str
    sender: str
    to: list[str]
    subject: str
    date: datetime | None
    urls: list[str] = field(default_factory=list)
    labels: list[str] | None = field(default_factory=list)   # None = unknown (IMAP: the folder is the label)
    has_query: bool = True   # the configured query matched subject or body (checked while the body was in memory)


# ---------------------------------------------------------------------------------------------- URL extraction
def _unwrap(url: str, depth: int = 0) -> str:
    """Follow redirect wrappers (google.com/url?q=, safelinks ?url=, ...) to the URL they point at."""
    if depth > 3:
        return url
    parts = urlsplit(url)
    for k, v in parse_qsl(parts.query, keep_blank_values=False):
        if k.lower() in _REDIRECT_KEYS:
            v = unquote(v)
            if v.lower().startswith(("http://", "https://")) and urlsplit(v).netloc != parts.netloc:
                return _unwrap(v, depth + 1)
    return url


def clean_url(raw: str) -> str | None:
    """Normalize one URL: unescape, unwrap redirects, strip trailing punctuation, tracking params and fragments."""
    u = html.unescape(raw.strip()).rstrip(_TRAIL)
    while u.endswith(")") and u.count("(") < u.count(")"):
        u = u[:-1]
    if not u.lower().startswith(("http://", "https://")):
        return None
    u = _unwrap(u)
    try:
        p = urlsplit(u)
    except ValueError:
        return None
    if not p.netloc:
        return None
    q = urlencode([(k, v) for k, v in parse_qsl(p.query, keep_blank_values=True) if not _TRACKING.match(k)])
    return urlunsplit((p.scheme.lower(), p.netloc.lower(), p.path or "/", q, ""))


def extract_urls(text: str) -> list[str]:
    """Every distinct http(s) URL in `text` (plain or HTML), in order of first appearance."""
    found = [m.group(1) for m in _HREF_RE.finditer(text)] + [m.group(0) for m in _URL_RE.finditer(text)]
    out: list[str] = []
    for raw in found:
        u = clean_url(raw)
        if u and u not in out:
            out.append(u)
    return out


class JobUrlFilter:
    """A URL is a job link when an enabled platform adapter claims its host, or its path/query looks like a posting.
    Job-looking URLs on hosts no adapter handles are kept on purpose: the run reports them as `unsupported_site`."""

    def __init__(self, cfg: IntakeMailCfg, supported: Callable[[str], bool] | None = None):
        self.job = [re.compile(p, re.IGNORECASE) for p in cfg.job_url_patterns]
        self.ignore = [re.compile(p, re.IGNORECASE) for p in cfg.ignore_url_patterns]
        self.supported = supported or (lambda _u: False)

    def __call__(self, url: str) -> bool:
        if any(p.search(url) for p in self.ignore):
            return False
        if self.supported(url):
            return True
        p = urlsplit(url)
        tail = p.path + ("?" + p.query if p.query else "")
        return any(r.search(tail) for r in self.job)


# ---------------------------------------------------------------------------------------------- parsing
def _bodies(msg: EmailMessage) -> Iterator[str]:
    for part in msg.walk():
        if part.get_content_maintype() != "text" or part.get_content_subtype() not in ("plain", "html"):
            continue
        if part.get_content_disposition() == "attachment":
            continue
        try:
            yield part.get_content()
        except (LookupError, UnicodeError, AssertionError, KeyError):
            continue


def message_key(msg_id: str, sender: str, date: str, subject: str) -> str:
    mid = msg_id.strip()
    if mid:
        return mid[:250]
    return "sha1:" + hashlib.sha1(f"{sender}|{date}|{subject}".encode()).hexdigest()


def parse_link_mail(raw: bytes, *, query: str = "", max_links: int = 20) -> LinkMail:
    m = email.message_from_bytes(raw, policy=policy.default)
    sender, subject, date = str(m.get("From", "")), str(m.get("Subject", "")), str(m.get("Date", ""))
    to = [a for _, a in getaddresses([str(m.get(h, "")) for h in ("To", "Cc", "Delivered-To") if m.get(h)]) if a]
    urls: list[str] = []
    q = query.lower().strip()
    hit = not q or q in subject.lower()
    for body in _bodies(m):            # transient: bodies never leave this function
        if q and not hit and q in body.lower():
            hit = True
        for u in extract_urls(body):
            if u not in urls:
                urls.append(u)
    labels = [x.strip() for x in str(m.get("X-Gmail-Labels", "")).split(",") if x.strip()]
    return LinkMail(key=message_key(str(m.get("Message-ID", "")), sender, date, subject), sender=sender, to=to,
                    subject=subject[:200], date=_parse_date(date), urls=urls[: max_links * 5], labels=labels,
                    has_query=hit)


def link_mail_from_dict(d: dict[str, Any], *, query: str = "", max_links: int = 20) -> LinkMail:
    """Client mode: the calling agent passes {id, from, to, subject, date, body}."""
    sender = str(d.get("from") or d.get("sender") or "")
    subject, body = str(d.get("subject") or ""), str(d.get("body") or d.get("snippet") or "")
    to = d.get("to") or []
    to = [a for _, a in getaddresses([to] if isinstance(to, str) else [str(x) for x in to]) if a]
    q = query.lower().strip()
    labels = d.get("labels") or []
    return LinkMail(key=message_key(str(d.get("id") or ""), sender, str(d.get("date") or ""), subject),
                    sender=sender, to=to, subject=subject[:200], date=_parse_date(d.get("date")),
                    urls=extract_urls(body)[: max_links * 5], labels=[str(x) for x in labels],
                    has_query=not q or q in subject.lower() or q in body.lower())


# ---------------------------------------------------------------------------------------------- filters
def _addr_match(want: str, addrs: Iterable[str]) -> bool:
    """Exact, case-insensitive full-address match only (never a substring or domain match)."""
    w = want.lower().strip()
    if "@" not in w:
        return False
    return any((parseaddr(a)[1] or a).lower().strip() == w for a in addrs)


@dataclass(slots=True)
class MailFilter:
    sender: str = ""          # resolved address or substring; "" = any
    recipient: str = ""
    query: str = ""
    label: str = ""
    since: datetime | None = None

    def __call__(self, m: LinkMail) -> bool:
        if self.since and m.date and m.date < self.since:
            return False
        if self.sender and not _addr_match(self.sender, [m.sender]):
            return False
        if self.recipient and not _addr_match(self.recipient, m.to):
            return False
        if self.label and m.labels is not None and self.label.lower() not in (x.lower() for x in m.labels):
            return False
        return m.has_query


CALL_OVERRIDES = ("query", "since", "since_days")   # a caller may only narrow; who and where come from config


def _full_address(v: str) -> bool:
    return bool(re.fullmatch(r"[^@\s<>,;]+@[^@\s<>,;]+\.[^@\s<>,;]+", v))


def resolve_filter(cfg: IntakeMailCfg, imap: ImapCfg, *, self_address: str = "",
                   overrides: dict[str, Any] | None = None) -> tuple[MailFilter, str]:
    """Build the filter from config + per-call overrides. Returns (filter, error). Fail closed (ADR-0045):
    - per-call overrides may only narrow (query, since, since_days); sender, recipient, self_address and label are
      config-only, so a caller cannot widen the read to other people's mail or other folders;
    - sender and recipient must each resolve to one of the user's own full addresses (`self` = self_address, else
      the profile contact email, else the IMAP user; plus intake.mail.self_aliases). Anything else is an error."""
    o = {k: v for k, v in (overrides or {}).items() if v is not None}
    bad = sorted(set(o) - set(CALL_OVERRIDES))
    if bad:
        return MailFilter(), f"mail overrides {bad} are not allowed per call (only {list(CALL_OVERRIDES)}); set them in config"
    me = (cfg.self_address or self_address or os.environ.get(imap.user_env, "")).strip().lower()
    own = {a.strip().lower() for a in [me, *cfg.self_aliases] if _full_address(a.strip())}

    def who(field_: str, v: str) -> tuple[str, str]:
        v = (v or "").strip().lower()
        if v == "self":
            v = me
        if not v:
            return "", f"intake.mail.{field_}: set intake.mail.self_address (or the profile contact email)"
        if not _full_address(v) or v not in own:
            return "", (f"intake.mail.{field_} must be 'self' or one of your own full addresses "
                        "(intake.mail.self_address / self_aliases)")
        return v, ""

    sender, e1 = who("sender", cfg.sender)
    recipient, e2 = who("recipient", cfg.recipient)
    if e1 or e2:
        return MailFilter(), e1 or e2
    since_s = o.get("since", cfg.since)
    if since_s:
        try:
            since = datetime.fromisoformat(str(since_s)).replace(tzinfo=UTC)
        except ValueError:
            return MailFilter(), f"bad since date {since_s!r} (want YYYY-MM-DD)"
    else:
        try:
            since = datetime.now(UTC) - timedelta(days=max(0, int(o.get("since_days", cfg.since_days))))
        except (TypeError, ValueError):
            return MailFilter(), "since_days must be a whole number"
    return MailFilter(sender=sender, recipient=recipient, query=str(o.get("query", cfg.query)),
                      label=cfg.label, since=since), ""


# ---------------------------------------------------------------------------------------------- sources
class MboxLinkSource:
    """mbox file or directory of .eml files, opened read-only (reuses ingest's reader)."""

    def __init__(self, path: str | Path, max_links: int = 20):
        self._src, self.max_links = MboxSource(path), max_links

    def fetch(self, flt: MailFilter, limit: int) -> Iterator[LinkMail]:
        n = 0
        for raw in self._src._raw():
            m = parse_link_mail(raw, query=flt.query, max_links=self.max_links)
            if flt(m):
                yield m
                n += 1
                if n >= limit:
                    return


def build_link_search(flt: MailFilter) -> list[str]:
    crit = ["SINCE", (flt.since or datetime.now(UTC)).strftime("%d-%b-%Y")]
    for key, val in (("FROM", flt.sender), ("TO", flt.recipient), ("TEXT", flt.query)):
        if val and val.isascii():
            crit += [key, '"' + val.replace("\\", "").replace('"', "") + '"']
    return crit


class ImapLinkSource:
    """IMAP, strictly read-only: EXAMINE, SEARCH and BODY.PEEK partial FETCH. No STORE, COPY, MOVE or EXPUNGE."""

    def __init__(self, cfg: ImapCfg, *, peek_bytes: int = 65536, max_links: int = 20, connect=None):
        self.cfg, self.peek, self.max_links = cfg, peek_bytes, max_links
        self._connect = connect   # injectable for tests

    def fetch(self, flt: MailFilter, limit: int) -> Iterator[LinkMail]:
        cfg = self.cfg
        if not cfg.host and self._connect is None:
            raise ValueError("ingest.imap.host is not configured")
        user, pw = _credentials(cfg) if self._connect is None else ("", "")
        conn = self._connect() if self._connect else imaplib.IMAP4_SSL(cfg.host, cfg.port)
        try:
            if self._connect is None:
                conn.login(user, pw)
            folder = flt.label or cfg.folder
            typ, _ = conn.select(f'"{folder}"', readonly=True)   # EXAMINE
            if typ != "OK":
                raise RuntimeError(f"cannot EXAMINE folder {folder!r}")
            typ, data = conn.search(None, *build_link_search(flt))
            ids = (data[0].split() if typ == "OK" and data and data[0] else [])[-limit:]
            for i in ids:
                typ, parts = conn.fetch(i, f"(BODY.PEEK[]<0.{self.peek}>)")
                if typ != "OK":
                    continue
                for part in parts:
                    if isinstance(part, tuple):
                        m = parse_link_mail(part[1], query=flt.query, max_links=self.max_links)
                        m.labels = None   # the EXAMINEd folder already is the label
                        if flt(m):
                            yield m
        finally:
            with contextlib.suppress(Exception):
                conn.logout()


class ClientLinkSource:
    def __init__(self, messages: Iterable[dict[str, Any]], max_links: int = 20):
        self.msgs, self.max_links = list(messages), max_links

    def fetch(self, flt: MailFilter, limit: int) -> Iterator[LinkMail]:
        n = 0
        for d in self.msgs:
            m = link_mail_from_dict(d, query=flt.query, max_links=self.max_links)
            if flt(m):
                yield m
                n += 1
                if n >= limit:
                    return


def make_link_source(settings: Any, messages: list[dict[str, Any]] | None = None, connect=None):
    """Source per intake.mail.source (default ingest.source). Returns None for client mode without messages."""
    mc, ic = settings.intake.mail, settings.ingest
    kind = "client" if messages is not None else (mc.source or ic.source)
    if kind == "client":
        return ClientLinkSource(messages, mc.max_links_per_message) if messages is not None else None
    if kind == "mbox":
        path = mc.path or ic.path
        if not path:
            raise ValueError("intake.mail.path (or ingest.path) is required for mail source 'mbox'")
        return MboxLinkSource(path, mc.max_links_per_message)
    if kind == "imap":
        return ImapLinkSource(ic.imap, peek_bytes=mc.peek_bytes, max_links=mc.max_links_per_message, connect=connect)
    raise ValueError(f"unknown mail source {kind!r} (mbox | imap | client)")

"""Read-only mail sources (ADR-0016).

Every source yields `Message` objects holding only sender, subject, date and a short snippet. Raw messages
are parsed in memory and dropped; bodies are never stored. No source can send, move, flag or delete mail:
mbox/eml files are opened for reading, IMAP uses EXAMINE and BODY.PEEK, and client mode is plain data.
"""
from __future__ import annotations

import contextlib
import email
import imaplib
import mailbox
import os
import re
from collections.abc import Iterable, Iterator
from datetime import UTC, datetime, timedelta
from email import policy
from email.message import EmailMessage
from pathlib import Path
from typing import Any, Protocol

from ..config import ImapCfg
from .confirmations import SNIPPET_MAX, Message, _parse_date


class MailSource(Protocol):
    """Blocking, read-only iterator of message summaries newer than `since`."""

    def fetch(self, since: datetime) -> Iterable[Message]: ...


def _snippet(msg: EmailMessage) -> str:
    part = msg.get_body(preferencelist=("plain", "html"))
    if part is None:
        return ""
    try:
        text = part.get_content()
    except (LookupError, UnicodeError, AssertionError):
        return ""
    if part.get_content_type() == "text/html":
        text = re.sub(r"(?is)<(script|style).*?</\1>", " ", text)
        text = re.sub(r"<[^>]+>", " ", text)
    return re.sub(r"\s+", " ", text).strip()[:SNIPPET_MAX]


def message_from_bytes(raw: bytes) -> Message:
    m = email.message_from_bytes(raw, policy=policy.default)
    return Message(sender=str(m.get("From", "")), subject=str(m.get("Subject", "")),
                   date=_parse_date(str(m.get("Date", ""))), snippet=_snippet(m),  # type: ignore[arg-type]
                   auth_results=tuple(str(h) for h in (m.get_all("Authentication-Results") or [])))


class ClientSource:
    """Messages supplied by the calling agent (default; no credentials live in the server)."""

    def __init__(self, messages: Iterable[dict[str, Any]], limit: int = 300):
        self._msgs = [Message.from_dict(m) for m, _ in zip(messages, range(limit), strict=False)]

    def fetch(self, since: datetime) -> Iterator[Message]:
        for m in self._msgs:
            if m.date is None or m.date >= since:
                yield m


class MboxSource:
    """A directory of .eml files, or an mbox file (opened read-only; never modified)."""

    def __init__(self, path: str | Path):
        self.path = Path(path).expanduser()

    def _raw(self) -> Iterator[bytes]:
        if self.path.is_dir():
            for f in sorted(self.path.rglob("*")):
                if f.suffix.lower() == ".eml" and f.is_file():
                    yield f.read_bytes()
                elif f.suffix.lower() == ".mbox" and f.is_file():
                    yield from self._mbox(f)
        elif self.path.is_file():
            yield from self._mbox(self.path)
        else:
            raise FileNotFoundError(f"mail path not found: {self.path}")

    @staticmethod
    def _mbox(path: Path) -> Iterator[bytes]:
        box = mailbox.mbox(str(path), create=False)
        try:
            for key in box.iterkeys():
                yield box.get_bytes(key)
        finally:
            box.close()

    def fetch(self, since: datetime) -> Iterator[Message]:
        for raw in self._raw():
            msg = message_from_bytes(raw)
            if msg.date is None or msg.date >= since:
                yield msg


def _credentials(cfg: ImapCfg) -> tuple[str, str]:
    """Username and password from the environment, else the OS keychain (optional `keyring` extra)."""
    user = os.environ.get(cfg.user_env, "")
    pw = os.environ.get(cfg.password_env, "")
    if user and not pw:
        try:
            import keyring  # lazy: optional extra

            pw = keyring.get_password(cfg.keyring_service, user) or ""
        except ImportError:
            pass
    if not (user and pw):
        raise PermissionError(
            f"IMAP credentials missing: set ${cfg.user_env} and ${cfg.password_env} (or store the password in "
            f"the OS keychain under service '{cfg.keyring_service}', `pip install job-apply-mcp[keyring]`)")
    return user, pw


def _or_chain(terms: list[tuple[str, str]]) -> list[str]:
    crit: list[str] = []
    for i, (key, val) in enumerate(terms):
        if i < len(terms) - 1:
            crit.append("OR")
        crit += [key, '"' + val.replace("\\", "").replace('"', "") + '"']
    return crit


_MONTHS = ("Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec")


def imap_date(d: datetime) -> str:
    """RFC 3501 date (`22-Oct-2026`). Month names are fixed English tokens in the protocol, so never use the
    process locale (`%b` is localized)."""
    return f"{d.day:02d}-{_MONTHS[d.month - 1]}-{d.year}"


def build_search(since: datetime, senders: list[str], subjects: list[str]) -> list[str]:
    crit = ["SINCE", imap_date(since)]
    terms = [("SUBJECT", s) for s in subjects if s.isascii() and s.strip()]
    terms += [("FROM", s) for s in senders if s.isascii() and s.strip()]
    return crit + (_or_chain(terms) if terms else [])


class ImapSource:
    """IMAP, strictly read-only: EXAMINE (select readonly), SEARCH, and BODY.PEEK partial FETCH only."""

    PEEK_BYTES = 16384

    def __init__(self, cfg: ImapCfg, *, connect=None):
        self.cfg = cfg
        self._connect = connect  # injectable for tests

    def fetch(self, since: datetime) -> Iterator[Message]:
        cfg = self.cfg
        if not cfg.host and self._connect is None:
            raise ValueError("ingest.imap.host is not configured")
        user, pw = _credentials(cfg) if self._connect is None else ("", "")
        conn = self._connect() if self._connect else imaplib.IMAP4_SSL(cfg.host, cfg.port)
        try:
            if self._connect is None:
                conn.login(user, pw)
            typ, _ = conn.select(f'"{cfg.folder}"', readonly=True)  # EXAMINE
            if typ != "OK":
                raise RuntimeError(f"cannot EXAMINE folder {cfg.folder!r}")
            typ, data = conn.search(None, *build_search(since, cfg.senders, cfg.subjects))
            ids = (data[0].split() if typ == "OK" and data and data[0] else [])[-cfg.max_messages:]
            for i in ids:
                typ, parts = conn.fetch(i, f"(BODY.PEEK[]<0.{self.PEEK_BYTES}>)")
                if typ != "OK":
                    continue
                for part in parts:
                    if isinstance(part, tuple):
                        msg = message_from_bytes(part[1])
                        if msg.date is None or msg.date >= since:
                            yield msg
        finally:
            with contextlib.suppress(Exception):  # best-effort close
                conn.logout()

    def latest_from(self, domain: str, since: datetime) -> Message | None:
        """ONLY the newest message FROM `domain` since `since` (ADR-0048): SEARCH, then FETCH the last match only.
        Sequence numbers grow with arrival, so the highest one is the newest message."""
        cfg = self.cfg
        if not cfg.host and self._connect is None:
            raise ValueError("ingest.imap.host is not configured")
        user, pw = _credentials(cfg) if self._connect is None else ("", "")
        conn = self._connect() if self._connect else imaplib.IMAP4_SSL(cfg.host, cfg.port)
        try:
            if self._connect is None:
                conn.login(user, pw)
            typ, _ = conn.select(f'"{cfg.folder}"', readonly=True)  # EXAMINE
            if typ != "OK":
                raise RuntimeError(f"cannot EXAMINE folder {cfg.folder!r}")
            typ, data = conn.search(None, *build_search(since, [domain], []))
            ids = data[0].split() if typ == "OK" and data and data[0] else []
            if not ids:
                return None
            newest = max(ids, key=int)
            typ, parts = conn.fetch(newest, f"(BODY.PEEK[]<0.{self.PEEK_BYTES}>)")
            if typ != "OK":
                return None
            for part in parts:
                if isinstance(part, tuple):
                    msg = message_from_bytes(part[1])
                    return msg if msg.date is not None and msg.date >= since else None
            return None
        finally:
            with contextlib.suppress(Exception):  # best-effort close
                conn.logout()


def since_dt(days: int) -> datetime:
    return datetime.now(UTC) - timedelta(days=max(0, days))


def make_source(kind: str, ingest_cfg) -> MailSource:
    if kind == "mbox":
        if not ingest_cfg.path:
            raise ValueError("ingest.path is required for source 'mbox'")
        return MboxSource(ingest_cfg.path)
    if kind == "imap":
        return ImapSource(ingest_cfg.imap)
    raise ValueError(f"source {kind!r} cannot be synced (client mode is push-only via ingest_confirmations)")

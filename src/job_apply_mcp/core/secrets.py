"""Secret values that cannot leak by accident (ADR-0046).

`Secret` wraps a credential so that str(), repr(), format(), f-strings, logging args, pickling and JSON all show a
fixed mask. Only `reveal()` returns the value, and only the browser fill call site uses it.

Defense in depth for every value that has been revealed in this process:
  * `redact(text)` replaces it with the mask (MCP tool output passes through it, see core/output.guarded);
  * a log-record factory redacts the formatted message and any exception text before handlers see the record.
The redaction set lives only in memory and only for values this process already holds.
"""
from __future__ import annotations

import hmac
import json
import logging
import re
import threading
from typing import Any, NoReturn
from urllib.parse import urlsplit, urlunsplit

MASK = "***"
_MIN_REDACT = 4                     # never redact trivially short strings (would mangle ordinary output)
_lock = threading.Lock()
_known: set[str] = set()


class Secret:
    """An opaque credential. Never printable; compare with ==, read with reveal()."""

    __slots__ = ("_v",)

    def __init__(self, value: str):
        if not isinstance(value, str) or not value:
            raise ValueError("secret must be a non-empty string")
        object.__setattr__(self, "_v", value)
        _remember(value)

    def reveal(self) -> str:
        return self._v

    def __setattr__(self, *_: Any) -> NoReturn:
        raise AttributeError("Secret is immutable")

    def __repr__(self) -> str:
        return f"Secret({MASK!r})"

    __str__ = __repr__

    def __format__(self, spec: str) -> str:
        return repr(self)

    def __eq__(self, other: object) -> bool:
        return isinstance(other, Secret) and hmac.compare_digest(self._v.encode(), other._v.encode())

    __hash__ = None  # type: ignore[assignment]

    def __len__(self) -> int:
        return len(self._v)

    def __reduce__(self) -> NoReturn:
        raise TypeError("Secret cannot be pickled or copied")

    def __bool__(self) -> bool:
        return True


def _remember(value: str) -> None:
    if len(value) >= _MIN_REDACT:
        with _lock:
            _known.add(value)


def forget(value: Secret | str) -> None:
    v = value.reveal() if isinstance(value, Secret) else value
    with _lock:
        _known.discard(v)


def redact(text: str) -> str:
    """Replace every secret this process holds with MASK. Longest first so overlapping values mask fully."""
    if not text or not _known:
        return text
    with _lock:
        vals = sorted(_known, key=len, reverse=True)
    for v in vals:
        for form in {v, json.dumps(v)[1:-1], json.dumps(v, ensure_ascii=False)[1:-1]}:   # raw and JSON-escaped
            if form in text:
                text = text.replace(form, MASK)
    return text


# ------------------------------------------------------------------ refs / URLs in error text (ADR-0074)

_URL_RE = re.compile(r"[a-zA-Z][a-zA-Z0-9+.-]*://[^\s'\"<>]+")


def safe_url(url: str) -> str:
    """scheme://host[:port]/path of a URL: no userinfo, query string or fragment (they can carry tokens)."""
    try:
        p = urlsplit(url)
        host = p.hostname or ""
        if p.port:
            host = f"{host}:{p.port}"
    except ValueError:              # unparsable (e.g. a bad port): keep only what precedes any '@', '?' or '#'
        return re.split(r"[@?#]", url, maxsplit=1)[0]
    if not p.scheme or not p.netloc:
        return re.split(r"[?#]", url, maxsplit=1)[0]
    return urlunsplit((p.scheme, host, p.path, "", ""))


def safe_ref(text: Any, limit: int = 120) -> str:
    """A caller-supplied ref or URL made safe to echo in an error: every URL in it reduced by `safe_url`, any
    remaining query/fragment dropped, known secrets redacted, then truncated to `limit` chars."""
    t = _URL_RE.sub(lambda m: safe_url(m.group(0)), str(text if text is not None else ""))
    if not _URL_RE.search(t):
        t = re.split(r"[?#]", t, maxsplit=1)[0]
    t = redact(t)
    return t if len(t) <= limit else t[: max(limit - 3, 0)] + "..."


def safe_text(text: Any, limit: int = 300) -> str:
    """An error message made safe to echo: URLs reduced by `safe_url`, known secrets redacted, truncated."""
    t = redact(_URL_RE.sub(lambda m: safe_url(m.group(0)), str(text if text is not None else "")))
    return t if len(t) <= limit else t[: max(limit - 3, 0)] + "..."


# ------------------------------------------------------------------ logging

_prev_factory = logging.getLogRecordFactory()
_fmt = logging.Formatter()


def _record_factory(*a: Any, **kw: Any) -> logging.LogRecord:
    rec = _prev_factory(*a, **kw)
    if not _known:
        return rec
    try:
        msg = rec.getMessage()
    except Exception:  # noqa: BLE001 - a bad format string is the caller's bug; keep the raw record
        return rec
    red = redact(msg)
    if red != msg:
        rec.msg, rec.args = red, None
    if rec.exc_info and not rec.exc_text:
        rec.exc_text = redact(_fmt.formatException(rec.exc_info))
    if rec.stack_info:
        rec.stack_info = redact(rec.stack_info)
    return rec


def install_log_redaction() -> None:
    """Idempotent: wrap the process log-record factory once."""
    if logging.getLogRecordFactory() is not _record_factory:
        global _prev_factory
        _prev_factory = logging.getLogRecordFactory()
        logging.setLogRecordFactory(_record_factory)


install_log_redaction()

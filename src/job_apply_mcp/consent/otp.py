"""otp_unattended (ADR-0048): fetch an emailed verification code with no human in the loop, inside a consent window.

Reads ONLY the newest message from the expected sender domain received within `grants.otp_window_min` minutes
(and not before the submit click), through the configured read-only mail source (ADR-0016). When that newest
message carries no code yet (e.g. an "application received" mail), it waits for a newer one, polling every
`grants.otp_poll_s` until `grants.otp_unattended_wait_s`. Older messages are never consulted.

The code lives only inside a `SecretCode`, whose str/repr/format are redacted and which cannot be pickled or
JSON-encoded. It is never logged, stored, audited or returned; callers `reveal()` it only as the argument that types
it into the page.
"""
from __future__ import annotations

import asyncio
import re
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any

from ..grants.otp import _domain_ok, pick_latest

# Cue words in common languages (the code itself is ASCII letters/digits everywhere). Matching is case-insensitive
# and needs only a Latin-letter boundary before the cue, so scripts without spaces (CJK) work too.
_CUES = ("code", "passcode", "otp", "pin", "one-time password", "one time password", "token",
         "código", "codigo", "codice", "kod", "kód", "koodi", "kode", "cod", "κωδικ", "код",
         "verifizierung", "bestätigung", "vérification", "verificación", "verificação", "verifica",
         "認証コード", "確認コード", "コード", "验证码", "驗證碼", "校验码", "인증번호", "인증 코드", "확인 코드",
         "رمز", "קוד", "कोड", "mã xác", "รหัส")
_CUE = re.compile("(?<![A-Za-z])(?:" + "|".join(re.escape(c) for c in sorted(_CUES, key=len, reverse=True)) + ")",
                  re.IGNORECASE)
_TOKEN = re.compile(r"(?<![A-Za-z0-9-])([A-Za-z0-9]{4,12})(?![A-Za-z0-9-])")
_DIGITS = re.compile(r"(?<![A-Za-z0-9-])(\d{6,8})(?![A-Za-z0-9-])")
_YEAR = re.compile(r"(19|20)\d\d")


def extract_code(subject: str, body: str) -> str | None:
    """A code from a verification mail: the first 4-12 character ASCII letters+digits token (at least one digit)
    within ~80 characters after a cue word in any of several languages. Language-neutral fallback for other
    wording: the ONLY 6-8 digit number in the mail. Anything ambiguous -> None (the caller fails closed)."""
    text = f"{subject}\n{body}"
    for cue in _CUE.finditer(text):
        for t in _TOKEN.finditer(text, cue.end(), min(len(text), cue.end() + 80)):
            tok = t.group(1)
            if any(ch.isdigit() for ch in tok) and not _YEAR.fullmatch(tok):
                return tok
    nums = {m.group(1) for m in _DIGITS.finditer(text)}
    return nums.pop() if len(nums) == 1 else None


class SecretCode:
    """Opaque holder: nothing but `reveal()` yields the value."""

    __slots__ = ("__v",)

    def __init__(self, value: str):
        self.__v = value

    def reveal(self) -> str:
        return self.__v

    def __repr__(self) -> str:
        return "SecretCode(<redacted>)"

    __str__ = __repr__

    def __format__(self, spec: str) -> str:
        return repr(self)

    def __reduce__(self):
        raise TypeError("SecretCode cannot be serialized")


@dataclass
class FetchResult:
    status: str                   # found | no_message | error
    code: SecretCode | None = field(default=None, repr=False)
    detail: str = ""              # never contains the code


def latest_from(source: Any, domain: str, since: datetime):
    """The single newest message from `domain` since `since`, or None. Uses the source's own targeted read when it
    has one (IMAP: SEARCH FROM, then FETCH only the last match)."""
    fn = getattr(source, "latest_from", None)
    if callable(fn):
        m = fn(domain, since)
        return m if m is not None and m.date is not None and m.date >= since and _domain_ok(m.sender, domain) else None
    return pick_latest(source.fetch(since), domain, since)


async def fetch_code(settings: Any, *, sender_domain: str, clicked_at: datetime, source: Any = None,
                     now: Callable[[], datetime] = lambda: datetime.now(UTC),
                     sleep: Callable[[float], Awaitable[Any]] = asyncio.sleep,
                     monotonic: Callable[[], float] = time.monotonic) -> FetchResult:
    g = settings.grants
    try:
        if source is None:
            from ..ingest.sources import make_source
            source = make_source(settings.ingest.source, settings.ingest)
    except Exception as e:  # noqa: BLE001 - no read-only mail source configured
        return FetchResult("error", detail=f"no usable mail source ({type(e).__name__})")
    deadline = monotonic() + max(0, g.otp_unattended_wait_s)
    while True:
        since = max(now() - timedelta(minutes=g.otp_window_min), clicked_at - timedelta(seconds=60))
        try:
            msg = await asyncio.to_thread(latest_from, source, sender_domain, since)
        except Exception as e:  # noqa: BLE001 - message dropped on purpose (could quote mail content)
            return FetchResult("error", detail=f"mail source error ({type(e).__name__})")
        if msg is not None:
            raw = extract_code(msg.subject, msg.snippet)
            del msg
            if raw is not None:
                return FetchResult("found", code=SecretCode(raw), detail=f"code mail from {sender_domain}")
        if monotonic() >= deadline:
            return FetchResult("no_message", detail=f"no code mail from {sender_domain} within "
                                                    f"{g.otp_unattended_wait_s}s")
        await sleep(max(1, g.otp_poll_s))

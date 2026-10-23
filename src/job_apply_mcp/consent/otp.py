"""otp_unattended (ADR-0048): fetch an emailed verification code with no human in the loop, inside a consent window.

The newest message must be DKIM- or DMARC-verified for that domain by YOUR mail server (`Authentication-Results`),
else the flow stops (`unknown`); a forged From never supplies a code. The mail settings themselves must come from the
trusted user config (`Settings._otp_mail_trusted`).

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


_COMMENT = re.compile(r"\([^()]*\)")
_RESINFO = re.compile(r"^\s*([A-Za-z0-9-]+)\s*=\s*([A-Za-z]+)")
_PROP = re.compile(r"([A-Za-z]+\.[A-Za-z0-9-]+)\s*=\s*\"?([^\s;\"]+)")


def _under(host: str, domain: str) -> bool:
    h, d = host.strip().lower().rstrip("."), domain.strip().lower().lstrip("@.").rstrip(".")
    return bool(h) and bool(d) and (h == d or h.endswith("." + d))


def parse_auth_results(header: str) -> tuple[str, list[tuple[str, str, dict[str, str]]]]:
    """RFC 8601 `Authentication-Results`: (authserv-id, [(method, result, {prop: value})])."""
    h = header
    for _ in range(5):                                  # strip (possibly nested) comments
        h = _COMMENT.sub(" ", h)
    parts = h.split(";")
    ident = parts[0].strip().split()[0].lower() if parts and parts[0].strip() else ""
    out = []
    for part in parts[1:]:
        m = _RESINFO.match(part)
        if m:
            out.append((m.group(1).lower(), m.group(2).lower(),
                        {k.lower(): v.lower() for k, v in _PROP.findall(part)}))
    return ident, out


def authenticated(msg: Any, domain: str, trusted_ids: list[str] | tuple[str, ...] = ()) -> bool:
    """Did the RECEIVING server verify this mail for `domain`? Needs `dkim=pass` with header.d (or header.i) equal
    to or under `domain`, or `dmarc=pass` with header.from equal to or under it. Only the topmost
    Authentication-Results header counts (the one your server added; lower ones may come from the sender), or, when
    `verification.trusted_authserv_ids` is set, only headers carrying one of those ids. Anything else: False."""
    headers = list(getattr(msg, "auth_results", ()) or ())
    ids = {i.strip().lower() for i in trusted_ids if i.strip()}
    picked = [h for h in headers if parse_auth_results(h)[0] in ids] if ids else headers[:1]
    for h in picked:
        for method, result, props in parse_auth_results(h)[1]:
            if result != "pass":
                continue
            if method == "dkim" and _under(props.get("header.d") or props.get("header.i", "").rpartition("@")[2],
                                           domain):
                return True
            if method == "dmarc" and _under(props.get("header.from", ""), domain):
                return True
    return False


@dataclass
class FetchResult:
    status: str                   # found | no_message | unauthenticated | error
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
            if not authenticated(msg, sender_domain, settings.verification.trusted_authserv_ids):
                del msg                                 # possibly forged: never read further, never wait it out
                return FetchResult("unauthenticated", detail=f"newest mail claiming {sender_domain} is not "
                                                             "DKIM/DMARC-verified by your mail server")
            raw = extract_code(msg.subject, msg.snippet)
            del msg
            if raw is not None:
                return FetchResult("found", code=SecretCode(raw), detail=f"code mail from {sender_domain}")
        if monotonic() >= deadline:
            return FetchResult("no_message", detail=f"no code mail from {sender_domain} within "
                                                    f"{g.otp_unattended_wait_s}s")
        await sleep(max(1, g.otp_poll_s))

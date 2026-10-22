"""otp_assist (ADR-0027): prefill an emailed verification code for a HUMAN to confirm. Never auto-submits.

Interface for the verification module (small on purpose):

    res = await assist_code(settings, sender_domain="example-ats.com", elicit=make_elicitor(ctx))
    res.status   # "confirmed" | "declined" | "fallback" | "no_grant" | "no_message" | "no_code" | "error"
    res.code     # only when status == "confirmed" (human clicked); otherwise None
    res.masked   # "••••12" for the human-only fallback path (provide_verification_code)

Reads ONLY the single most recent message from the expected sender domain inside the last N minutes, through the
configured read-only mail source. The code is never stored, logged, or put into an audit line.
"""
from __future__ import annotations

import asyncio
import re
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from email.utils import parseaddr
from typing import Any

from pydantic import BaseModel, Field

from .store import GrantError, store_for

Elicit = Callable[[str], Awaitable["bool | None"]]    # True = human confirmed, False = declined, None = unsupported
_CODE = re.compile(r"(?<![\w-])(\d{4,8})(?![\w-])")
_CUE = re.compile(r"(?i)(code|otp|verification|passcode|security|pin)")


@dataclass
class OtpResult:
    status: str
    code: str | None = field(default=None, repr=False)     # repr hidden so it cannot leak into logs
    masked: str = ""
    sender: str = ""
    received: str = ""
    detail: str = ""


def mask(code: str) -> str:
    return "••••" + code[-2:]


def _domain_ok(sender: str, domain: str) -> bool:
    host = parseaddr(sender)[1].rpartition("@")[2].lower()
    d = domain.lower().lstrip("@.")
    return bool(d) and (host == d or host.endswith("." + d))


def extract_code(subject: str, body: str) -> str | None:
    text = f"{subject}\n{body}"
    best = None
    for m in _CODE.finditer(text):
        near = text[max(0, m.start() - 60):m.end() + 20]
        if _CUE.search(near):
            return m.group(1)
        best = best or m.group(1)
    return best


def pick_latest(messages, domain: str, since: datetime):
    cand = [m for m in messages if m.date is not None and m.date >= since and _domain_ok(m.sender, domain)]
    return max(cand, key=lambda m: m.date) if cand else None


class _Confirm(BaseModel):
    submit: bool = Field(description="Submit this code?")


def make_elicitor(ctx: Any) -> Elicit:
    """Wrap an MCP Context's elicitation. Returns None (fallback) when the client cannot elicit."""
    async def ask(message: str) -> bool | None:
        try:
            res = await ctx.elicit(message=message, schema=_Confirm)
        except Exception:  # noqa: BLE001 - client without elicitation support
            return None
        return bool(res.action == "accept" and res.data.submit)
    return ask


async def assist_code(settings, *, sender_domain: str, elicit: Elicit | None, source: Any = None,
                      now: datetime | None = None) -> OtpResult:
    st = store_for(settings)
    try:
        g = st.check("otp_assist", detail=f"sender_domain={sender_domain}")
    except GrantError as e:
        return OtpResult("no_grant", detail=str(e)[:200])
    now = now or datetime.now(UTC)
    since = now - timedelta(minutes=settings.grants.otp_window_min)
    try:
        if source is None:
            from ..ingest.sources import make_source
            source = make_source(settings.ingest.source, settings.ingest)
        msgs = await asyncio.to_thread(lambda: list(source.fetch(since)))
    except Exception as e:  # noqa: BLE001
        st.audit_safe("otp_error", g.id, "otp_assist", type(e).__name__)
        return OtpResult("error", detail=f"mail source: {type(e).__name__}")
    msg = pick_latest(msgs, sender_domain, since)
    if msg is None:
        st.audit_safe("otp_none", g.id, "otp_assist", f"no recent message from {sender_domain}")
        return OtpResult("no_message", detail=f"no message from {sender_domain} in the last "
                         f"{settings.grants.otp_window_min} min")
    code = extract_code(msg.subject, msg.snippet)
    if code is None:
        st.audit_safe("otp_none", g.id, "otp_assist", "message had no code")
        return OtpResult("no_code", sender=msg.sender, detail="latest message contained no code")
    when = msg.date.astimezone().strftime("%H:%M")
    res = OtpResult("fallback", masked=mask(code), sender=msg.sender, received=when)
    verdict = await elicit(f"Code from {msg.sender} at {when}: {mask(code)} — submit?") if elicit else None
    if verdict is True:
        res.status, res.code = "confirmed", code
    elif verdict is False:
        res.status = "declined"
    st.audit_safe(f"otp_{res.status}", g.id, "otp_assist", f"sender_domain={sender_domain}")
    return res

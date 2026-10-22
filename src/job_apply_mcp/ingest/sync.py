"""Write parsed confirmations to the store so `seen()` blocks re-applying (ADR-0016)."""
from __future__ import annotations

from collections.abc import Iterable
from datetime import UTC, datetime, timedelta
from typing import Any, Protocol

from ..core.models import SubmitResult
from .confirmations import ConfirmationParser, Message


class StoreLike(Protocol):
    async def seen(self, company: str, title: str, *, within_days: int = ...) -> bool: ...
    async def record(self, result: SubmitResult, *, company: str, title: str) -> None: ...


async def ingest_messages(store: StoreLike, parser: ConfirmationParser, messages: Iterable[Message], *,
                          min_confidence: float = 0.6, dedupe_days: int = 180, max_new_shown: int = 10,
                          preview: bool = False) -> dict[str, Any]:
    """Compact result: counts plus at most `max_new_shown` new matches. Idempotent: the record ref is a hash of
    sender+subject+day, and a confirmation whose company+title is already seen() is counted as `known`."""
    cutoff = datetime.now(UTC) - timedelta(days=dedupe_days)
    scanned = matched = new = known = low = old = 0
    shown: list[dict[str, Any]] = []
    for msg in messages:
        scanned += 1
        c = parser.parse(msg)
        if c is None:
            continue
        matched += 1
        if c.confidence < min_confidence:
            low += 1
            continue
        if c.date < cutoff:
            old += 1  # outside the dedupe window: would not block anything
            continue
        if await store.seen(c.company, c.title, within_days=dedupe_days):
            known += 1
            continue
        if not preview:
            await store.record(
                SubmitResult(ref=c.ref, status="submitted", via="email", confirmation=f"email:{c.platform}",
                             detail=f"mail confirmation {c.date.date().isoformat()} {c.sender_domain} "
                                    f"conf={c.confidence:.2f}"),
                company=c.company, title=c.title)
        new += 1
        if len(shown) < max_new_shown:
            shown.append(c.row())
    out: dict[str, Any] = {"scanned": scanned, "matched": matched, "new": new, "known": known,
                           "low_conf": low, "outside_window": old, "new_rows": shown}
    if new > len(shown):
        out["more"] = new - len(shown)
    if preview:
        out["preview"] = True
    return out


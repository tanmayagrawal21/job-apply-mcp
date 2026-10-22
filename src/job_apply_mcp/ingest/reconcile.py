"""Close the loop between submissions and confirmation emails (ADR-0025).

Read-only on mail. Reconciliation can only (a) move a submission to the terminal `confirmed` state with the
email's subject/date as evidence, or (b) annotate it. It never resubmits and never touches `failed`.
"""
from __future__ import annotations

import time
from collections.abc import Iterable
from datetime import UTC, datetime, timedelta
from typing import Any

from ..core.models import SubmitResult
from ..store.sqlite import company_key, titles_similar
from .confirmations import Confirmation, ConfirmationParser, Message

SLACK = timedelta(minutes=5)       # email clocks vs. server clock
CLIENT_PLATFORMS_NEUTRAL = ("employer",)


def platform_of(ref: str) -> str:
    parts = ref.split(":", 2)
    return parts[0].lower() if len(parts) >= 3 and not ref.lower().startswith(("http:", "https:")) else ""


def match_one(sub: dict[str, Any], c: Confirmation, *, window: timedelta, threshold: float) -> bool:
    """Platform + company identity + title similarity + time window; all must hold."""
    plat = platform_of(sub["ref"])
    if plat and c.platform not in CLIENT_PLATFORMS_NEUTRAL and c.platform != plat:
        return False  # a Lever mail cannot confirm a Greenhouse submission
    if not company_key(sub["company"]) or company_key(sub["company"]) != company_key(c.company):
        return False
    if not titles_similar(sub["title"], c.title, threshold):
        return False
    t0 = datetime.fromtimestamp(sub["t0"], UTC)
    return t0 - SLACK <= c.date <= t0 + window


def match(subs: list[dict[str, Any]], pairs: list[tuple[Message, Confirmation]], *, window: timedelta,
          threshold: float) -> tuple[dict[str, tuple[Message, Confirmation]], int]:
    """-> ({ref: (message, confirmation)}, ambiguous_count). An email matching several submissions, or a
    submission matching emails from different days, is ambiguous: no state change for those."""
    hits: dict[str, list[int]] = {}
    owners: dict[int, list[str]] = {}
    for i, (_, c) in enumerate(pairs):
        for s in subs:
            if match_one(s, c, window=window, threshold=threshold):
                hits.setdefault(s["ref"], []).append(i)
                owners.setdefault(i, []).append(s["ref"])
    out: dict[str, tuple[Message, Confirmation]] = {}
    amb = 0
    for ref, lst in hits.items():
        if any(len(owners[i]) > 1 for i in lst):
            amb += 1
            continue
        out[ref] = pairs[min(lst, key=lambda i: pairs[i][1].date)]
    return out, amb


def client_task(since: datetime, n_open: int, limit: int) -> dict[str, Any]:
    return {"task": "fetch_confirmation_emails", "since": since.date().isoformat(), "max": limit,
            "instruction": ("Using your own mail tool (read-only), list application-confirmation emails received "
                            f"since {since.date().isoformat()} and pass [{{sender, subject, date, snippet}}] to "
                            "ingest_confirmations. Do not send, move or delete mail."),
            "open_submissions": n_open}


async def reconcile(book, store, settings, *, parser: ConfirmationParser, messages: Iterable[Message] | None,
                    force: bool = False, now: float | None = None) -> dict[str, Any]:
    """Match `messages` (already fetched by the caller) against open submissions. Pass messages=None to only
    inspect what is due (returns what the caller needs to fetch)."""
    cfg = settings.ingest
    now = time.time() if now is None else now
    opened = book.open_reconciliations()
    live = [o for o in opened if o["pending"]]
    due = [o for o in live if o["due"] is not None and o["due"] <= now]
    out: dict[str, Any] = {"open": len(live), "due": len(due)}
    nxt = [o["due"] for o in live if o["due"] is not None and o["due"] > now]
    if nxt:
        out["next_due_min"] = max(0, round((min(nxt) - now) / 60, 1))
    if messages is None:
        return out
    if not (due or force):
        out["note"] = "nothing due; pass force=true to check now"
        return out
    # Match against every open submission (an early email can confirm a later window), not only the due ones.
    subs = [o for o in opened if o["state"] != "confirmed"]
    pairs: list[tuple[Message, Confirmation]] = []
    for m in messages:
        c = parser.parse(m)
        if c is not None and c.confidence >= cfg.min_confidence:
            pairs.append((m, c))
    found, amb = match(subs, pairs, window=timedelta(hours=cfg.reconcile_window_hours),
                       threshold=cfg.reconcile_title_threshold)
    confirmed: list[dict[str, Any]] = []
    for ref, (m, c) in found.items():
        prev = next(o["state"] for o in subs if o["ref"] == ref)
        subj = " ".join(m.subject.split())[:120]
        ev = f"confirmed by email {c.date.date().isoformat()} | {subj} | {c.sender_domain}"
        if book.confirm(ref, ev):
            sub = next(o for o in subs if o["ref"] == ref)
            if prev != "submitted":
                await store.record(SubmitResult(ref=ref, status="submitted", via="email",
                                                confirmation=f"email:{c.platform}", detail=ev[:200]),
                                   company=sub["company"], title=sub["title"])
            confirmed.append({"ref": ref, "from": prev, "co": sub["company"], "title": sub["title"],
                              "mail_date": c.date.date().isoformat(), "subject": subj})
    book.mark_checked([o["ref"] for o in due if o["ref"] not in found])
    unresolved = []
    ran = {o["ref"] for o in due}
    for o in book.open_reconciliations():
        if o["ref"] in ran and o["pending"] == 0 and o["state"] in ("unknown", "submitting", "handoff"):
            book.annotate(o["ref"], "no confirmation email found after the last check; state unchanged")
            unresolved.append({"ref": o["ref"], "state": o["state"], "co": o["company"], "title": o["title"]})
    out.update({"checked": len(pairs), "confirmed": confirmed, "unresolved": unresolved[:cfg.max_new_shown]})
    if amb:
        out["ambiguous"] = amb
    out["open"] = len([o for o in book.open_reconciliations() if o["pending"]])
    return out


def pending_summary(book, now: float | None = None) -> dict[str, Any]:
    now = time.time() if now is None else now
    live = [o for o in book.open_reconciliations() if o["pending"]]
    due = [o for o in live if o["due"] <= now]
    out: dict[str, Any] = {"open": len(live), "due": len(due)}
    if live and not due:
        out["next_due_min"] = round((min(o["due"] for o in live) - now) / 60, 1)
    return out

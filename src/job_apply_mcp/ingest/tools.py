"""MCP tools for mail-confirmation dedupe. `register(mcp, get_svc)` is the only wiring server.py needs."""
from __future__ import annotations

import asyncio
import json
from collections.abc import Callable
from typing import Any

from .confirmations import ConfirmationParser, Message, load_patterns
from .reconcile import client_task, reconcile
from .sources import ClientSource, make_source, since_dt
from .sync import ingest_messages

_parser: tuple[str | None, ConfirmationParser] | None = None


def get_parser(patterns_file: str | None) -> ConfirmationParser:
    global _parser
    if _parser is None or _parser[0] != patterns_file:
        _parser = (patterns_file, ConfirmationParser(load_patterns(patterns_file)))
    return _parser[1]


async def sync(store, settings, *, source: str | None = None, since_days: int | None = None,
               messages: list[dict[str, Any]] | None = None, preview: bool = False) -> dict[str, Any]:
    cfg = settings.ingest
    kind = source or ("client" if messages is not None else cfg.source)
    parser = get_parser(cfg.patterns_file)
    days = since_days if since_days is not None else cfg.since_days
    if kind == "client":
        msgs: list[Message] = list(ClientSource(messages or [], cfg.max_messages_per_call).fetch(since_dt(days)))
    else:
        src = make_source(kind, cfg)
        msgs = await asyncio.to_thread(lambda: list(src.fetch(since_dt(days))))
    out = await ingest_messages(store, parser, msgs, min_confidence=cfg.min_confidence,
                                dedupe_days=settings.policy.dedupe_days, max_new_shown=cfg.max_new_shown,
                                preview=preview)
    out["source"] = kind
    return out


def _book(svc: Any) -> Any:
    try:
        return svc.engine.approvals
    except Exception:  # noqa: BLE001 - apply engine optional/unavailable: reconciliation is simply skipped
        return None


async def reconcile_now(svc, *, messages: list[dict[str, Any]] | None = None, source: str | None = None,
                        force: bool = False) -> dict[str, Any]:
    """Reconcile open submissions with confirmation mail. client source without `messages` -> a client_task."""
    cfg = svc.settings.ingest
    book = _book(svc)
    if book is None:
        return {"open": 0, "note": "apply engine unavailable"}
    kind = source or ("client" if messages is not None else cfg.source)
    parser = get_parser(cfg.patterns_file)
    status = await reconcile(book, svc.store, svc.settings, parser=parser, messages=None)
    if not status["open"]:
        return status
    if kind == "client" and messages is None:
        status["client_task"] = client_task(since_dt(cfg.reconcile_window_hours // 24 + 2), status["open"],
                                            cfg.max_messages_per_call)
        return status
    if kind == "client":
        msgs = list(ClientSource(messages or [], cfg.max_messages_per_call).fetch(since_dt(cfg.since_days)))
    else:
        src = make_source(kind, cfg)
        msgs = await asyncio.to_thread(lambda: list(src.fetch(since_dt(cfg.reconcile_window_hours // 24 + 2))))
    return await reconcile(book, svc.store, svc.settings, parser=parser, messages=msgs, force=force)


def register(mcp: Any, get_svc: Callable[[], Any]) -> None:
    def _dump(o: Any) -> str:
        return json.dumps(o, separators=(",", ":"), default=str)

    @mcp.tool()
    async def ingest_confirmations(messages: list[dict[str, Any]], preview: bool = False) -> str:
        """Record the user's own application-confirmation emails so those jobs are never re-applied to.
        Pass message summaries from the user's mail tool: [{sender, subject, date, snippet}] (snippet = first
        ~500 chars, optional). Read-only on mail; the server stores only company, title and date. Returns
        counts plus up to a few new matches. preview=true parses without writing."""
        s = get_svc()
        try:
            out = await sync(s.store, s.settings, messages=messages, preview=preview)
            if not preview:
                rec = await reconcile_now(s, messages=messages, force=True)
                if rec.get("confirmed") or rec.get("unresolved"):
                    out["reconcile"] = {k: rec[k] for k in ("confirmed", "unresolved", "ambiguous") if rec.get(k)}
            return _dump(out)
        except (ValueError, OSError) as e:
            return _dump({"error": str(e)})

    @mcp.tool()
    async def sync_confirmations(since_days: int | None = None, source: str | None = None,
                                 preview: bool = False) -> str:
        """Pull confirmation emails from the configured read-only source (ingest.source: mbox or imap) for the
        last `since_days` days and record them as already applied. Never sends, moves, flags or deletes mail."""
        s = get_svc()
        try:
            return _dump(await sync(s.store, s.settings, source=source, since_days=since_days, preview=preview))
        except (ValueError, OSError, PermissionError, RuntimeError) as e:
            return _dump({"error": str(e)})

    @mcp.tool()
    async def reconcile_submissions(messages: list[dict[str, Any]] | None = None, force: bool = False) -> str:
        """Match open submissions (submitted/unknown/submitting/handoff) to confirmation emails and mark confident
        matches `confirmed` (evidence = email subject/date; never bodies). Checks run at ingest.reconcile_delays_min
        after each submit; force=true checks now. With the default client mail source and no `messages`, returns a
        `client_task`: fetch recent confirmation-email summaries and pass them here or to ingest_confirmations.
        An unknown with no match after the last window stays unknown and is listed under `unresolved`. Read-only
        on mail; never resubmits."""
        s = get_svc()
        try:
            return _dump(await reconcile_now(s, messages=messages, force=force))
        except (ValueError, OSError, PermissionError, RuntimeError) as e:
            return _dump({"error": str(e)})

    @mcp.tool()
    async def seed_from_confirmations(since_days: int | None = None, messages: list[dict[str, Any]] | None = None,
                                      preview: bool = False) -> str:
        """Seed dedupe from confirmation-email history (default ingest.seed_since_days=180) before a run, so no
        side files are needed. mbox/imap sources are read directly; with the client source and no `messages`
        this returns a `client_task` to fetch summaries and call it again with them."""
        s = get_svc()
        cfg = s.settings.ingest
        days = since_days if since_days is not None else cfg.seed_since_days
        try:
            if cfg.source == "client" and messages is None:
                return _dump({"client_task": client_task(since_dt(days), 0, cfg.max_messages_per_call)})
            return _dump(await sync(s.store, s.settings, since_days=days, messages=messages, preview=preview))
        except (ValueError, OSError, PermissionError, RuntimeError) as e:
            return _dump({"error": str(e)})

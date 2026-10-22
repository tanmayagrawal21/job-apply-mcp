"""MCP tools for mail-confirmation dedupe. `register(mcp, get_svc)` is the only wiring server.py needs."""
from __future__ import annotations

import asyncio
import json
from collections.abc import Callable
from typing import Any

from .confirmations import ConfirmationParser, Message, load_patterns
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
            return _dump(await sync(s.store, s.settings, messages=messages, preview=preview))
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

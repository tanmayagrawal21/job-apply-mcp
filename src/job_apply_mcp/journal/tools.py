"""MCP tools for the candidate journal (ADR-0064) and the client instructions that ask for them."""
from __future__ import annotations

import json
from collections.abc import Callable
from typing import Any

from mcp.server.fastmcp import Context
from pydantic import BaseModel, Field

from ..config import Settings, state_path
from .store import CandidateJournal

# Appended to the server instructions (server.py) as a separate block, so edits to the main text merge cleanly.
CLIENT_INSTRUCTIONS = """\
Whenever the user states a fact about themselves (residence, authorization, visa, availability, salary, \
preferences, employers, constraints, stories) or corrects an answer, call record_candidate_fact at once: one fact \
per call, with their exact words in `quote`. Use confidence=inferred for your own conclusions, and kind=story for \
experiences. Never record passwords, codes, tokens, SSNs or card numbers."""


class _ConfirmFact(BaseModel):
    correct: bool = Field(description="Is this correct, and may it be used in your job applications?")


def journal_for(settings: Settings) -> CandidateJournal:
    return CandidateJournal(state_path(settings))


def _dump(obj: Any) -> str:
    return json.dumps(obj, separators=(",", ":"), ensure_ascii=False)


async def confirm_by_elicitation(ctx: Any, text: str) -> bool | None:
    """True/False when the user answered the dialog; None when the client cannot elicit (or no context)."""
    if ctx is None:
        return None
    try:
        res = await ctx.elicit(message=text[:1500], schema=_ConfirmFact)
    except Exception:  # noqa: BLE001 - client without elicitation support
        return None
    if res.action == "accept" and res.data is not None:
        return bool(res.data.correct)
    return False


async def record(settings: Settings, ctx: Any, *, fact: str, key: str = "", value: str = "", scope: str = "global",
                 source: str = "chat", quote: str = "", confidence: str = "stated", client: str = "",
                 kind: str = "fact") -> dict[str, Any]:
    from .store import normalize_key, risk_class
    j = journal_for(settings)
    elicited: bool | None = None
    nk = normalize_key(key) or ""
    if kind != "story" and confidence != "inferred" and risk_class(nk, f"{fact} {value}") == "sensitive":
        shown = f"{fact.strip()[:600]}" + (f"\n({nk} = {value.strip()[:80]})" if nk and value.strip() else "")
        elicited = await confirm_by_elicitation(
            ctx, "Save this for your future job applications? It may be used to answer authorization, visa or "
                 f"other sensitive questions.\n\n{shown}")
    try:
        e, warnings = j.record(fact, key=key, value=value, scope=scope, source=source, quote=quote,
                               confidence=confidence, client=client, kind=kind, elicited=elicited)
    except ValueError as err:
        return {"ok": False, "error": {"code": "not_recorded", "message": str(err)[:200],
                                       "hint": "record the fact without the secret"}}
    out: dict[str, Any] = {"ok": True, **e.compact()}
    out.pop("quote", None)
    if e.risk == "sensitive" and not e.confirmed_via:
        out["note"] = ("recorded, not yet usable on forms: sensitive facts need the user's confirmation (dialog) or "
                       "their verbatim quote with source=chat or correction and confidence=stated")
    elif e.risk == "attestation":
        out["note"] = "recorded; attestations are never filled automatically"
    if warnings:
        out["warnings"] = warnings
    return out


def list_facts(settings: Settings, *, key_prefix: str = "", kind: str = "", risk: str = "",
               include_retracted: bool = False, limit: int = 25, cursor: int = 0, full: bool = False) -> dict[str, Any]:
    rows = journal_for(settings).entries(include_retracted=include_retracted)
    rows = [e for e in rows if (not key_prefix or e.key.startswith(key_prefix.strip().lower()))
            and (not kind or e.kind == kind) and (not risk or e.risk == risk)]
    rows.reverse()                                  # newest first
    limit = max(1, min(int(limit), 100))
    page = rows[cursor:cursor + limit]
    nxt = cursor + limit if cursor + limit < len(rows) else None
    return {"rows": [e.compact(full=full) for e in page], "total": len(rows), "next": nxt}


def register(mcp: Any, get_settings: Callable[[], Settings]) -> None:
    @mcp.tool()
    async def record_candidate_fact(ctx: Context, fact: str, key: str = "", value: str = "", scope: str = "global",
                                    source: str = "chat", quote: str = "", confidence: str = "stated",
                                    client: str = "", kind: str = "fact") -> str:
        """Log something the user said about themselves into the dated, append-only candidate journal, so later
        applications can use it. CALL THIS whenever the user states a fact about themselves (residence, work
        authorization, visa, citizenship, availability, salary, preferences, past employers, constraints), corrects
        an answer, or tells a story about their work. One fact per call; never blocked.
        fact: one sentence in plain words. key/value: optional structured form, e.g.
        authorization.US.requires_sponsorship / yes, authorization.home_country / US, preferences.willing_to_relocate
        / no, availability.start_date / 2027-01-04, employment.prior_employers / Example Corp, demographics.<k>.
        scope: global | country:XX | company:<name> | role:<title>. source: chat | correction | document.
        quote: the user's exact words. confidence: stated | inferred | confirmed (inferred never fills a form).
        client: your client name. kind: fact | story (stories are essay material, retrieved for prose questions).
        Sensitive facts (authorization, visa, citizenship, sponsorship, EEO, criminal history, clearance) may ask the
        user to confirm; without a dialog, only a verbatim quote (source=chat or correction, confidence=stated) counts.
        Passwords, codes, tokens, SSNs and card numbers are redacted or refused."""
        return _dump(await record(get_settings(), ctx, fact=fact, key=key, value=value, scope=scope, source=source,
                                  quote=quote, confidence=confidence, client=client, kind=kind))

    @mcp.tool()
    async def list_candidate_facts(key_prefix: str = "", kind: str = "", risk: str = "",
                                   include_retracted: bool = False, limit: int = 25, cursor: int = 0,
                                   full: bool = False) -> str:
        """The candidate journal, newest first: id, date, fact, key/value, scope, source, confidence, risk
        (everyday | sensitive | attestation), confirmed_via and `usable` (whether it may fill a form). Filter with
        key_prefix (e.g. "authorization."), kind (fact | story) or risk. Check it before asking the user something
        they may already have answered. Page with cursor=next."""
        return _dump(list_facts(get_settings(), key_prefix=key_prefix, kind=kind, risk=risk,
                                include_retracted=include_retracted, limit=limit, cursor=cursor, full=full))

    @mcp.tool()
    async def retract_candidate_fact(id: str, reason: str = "", client: str = "") -> str:
        """Withdraw a journal entry the user says is wrong. Adds a dated tombstone; nothing is deleted. Record the
        correct fact with record_candidate_fact (source=correction)."""
        try:
            t = journal_for(get_settings()).retract(id, reason, client)
        except ValueError as err:
            return _dump({"ok": False, "error": {"code": "not_found", "message": str(err)[:200],
                                                 "hint": "list_candidate_facts shows entry ids"}})
        return _dump({"ok": True, "retracted": id, "tombstone": t.id, "date": t.date})

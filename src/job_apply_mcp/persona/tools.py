"""MCP tools for the profile builder (ADR-0047) and the start-of-session interactive round.

`register(mcp, get_svc)` is the only wiring server.py needs for the two tools; `ask_at_start(ctx, svc)` is the
helper `plan_application` and `apply_batch` call BEFORE any planning, so a batch is never interrupted mid-run.
"""
from __future__ import annotations

import json
from collections.abc import Callable
from typing import Any

from pydantic import Field, create_model

from .gaps import SCOPES, ProfileDB, shared

_asked = False      # one interactive round per server process (= per client session)


def db_for(settings: Any) -> ProfileDB:
    return shared(settings.paths.data_dir)


def parse_scope(scope: str, scope_value: str = "") -> tuple[str, str]:
    """'global' | 'company' | 'role_family', optionally 'company:Acme' / 'role_family:data'."""
    s, _, v = scope.partition(":")
    s = s.strip().lower().replace("-", "_")
    if s not in SCOPES:
        raise ValueError(f"scope must be one of {', '.join(SCOPES)}")
    return s, (scope_value or v).strip()


def store_answer(settings: Any, key: str, value: str, scope: str = "global", scope_value: str = "") -> dict[str, Any]:
    s, v = parse_scope(scope, scope_value)
    key = key.strip()
    if not key or not value.strip():
        raise ValueError("key and value are required")
    db_for(settings).set_answer(key, value.strip(), s, v, origin="user_written")
    return {"ok": True, "key": key, "scope": s + (f":{v}" if v else ""), "origin": "user_written"}


def questions(settings: Any, limit: int) -> list[dict[str, Any]]:
    rows = db_for(settings).questions(limit=limit * 3, include_skipped=False)
    mn = settings.persona.min_hits
    return [r for r in rows if r["n"] >= mn][:max(1, limit)]


async def ask_at_start(ctx: Any, get_svc: Callable[[], Any]) -> int:
    """Ask the top gaps over MCP elicitation, once. Returns the number answered. Silent when the client cannot
    elicit, when disabled, or when nothing is open."""
    global _asked
    if _asked:
        return 0
    _asked = True
    try:
        st = get_svc().settings
        if not st.persona.enabled or not st.persona.ask_on_start:
            return 0
        rows = questions(st, st.persona.ask_limit)
        if not rows:
            return 0
        fields: dict[str, Any] = {}
        for i, r in enumerate(rows):
            note = f" (now defaulting to: {r['default_used']})" if r.get("default_used") else ""
            fields[f"a{i}"] = (str | None, Field(default=None, description=f"{r['question'][:160]}{note}. "
                                                 f"Seen on {r['n']} application(s). Leave empty to skip."))
        schema = create_model("ProfileQuestions", **fields)
        res = await ctx.elicit(message="A few answers would let me fill more applications on my own. Anything you "
                                       "type is saved to your profile and reused; leave blank to skip.",
                               schema=schema)
    except Exception:  # noqa: BLE001 - client without elicitation support, or any failure: never block the run
        return 0
    db = db_for(get_svc().settings)
    done = 0
    for i, r in enumerate(rows):
        val = (getattr(res.data, f"a{i}", None) or "").strip() if res.action == "accept" and res.data else ""
        if val:
            db.set_answer(r["key"], val, "global", origin="user_written")
            done += 1
        else:
            db.set_status(r["key"], "skipped")
    return done


def register(mcp: Any, get_svc: Callable[[], Any]) -> None:
    @mcp.tool()
    async def profile_questions(limit: int = 5) -> str:
        """Top open profile gaps (questions the tool could not answer, or answered with a default), ranked by
        frequency x impact (blocked applications weigh most). Ask the user these, then call answer_profile_question."""
        return json.dumps({"questions": questions(get_svc().settings, limit)}, separators=(",", ":"))

    @mcp.tool()
    async def answer_profile_question(key: str, value: str, scope: str = "global", scope_value: str = "") -> str:
        """Store the USER's answer for a gap key (from profile_questions) with provenance user_written. scope:
        global | company | role_family (give scope_value: the company name or role family, or pass 'company:Acme').
        Applies to all future plans. Only relay what the user actually said; never invent an answer."""
        try:
            out = store_answer(get_svc().settings, key, value, scope, scope_value)
        except ValueError as e:
            out = {"ok": False, "error": str(e)}
        return json.dumps(out, separators=(",", ":"))

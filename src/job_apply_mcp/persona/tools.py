"""MCP tools for the profile builder (ADR-0047) and the start-of-session interactive round.

`register(mcp, get_svc)` is the only wiring server.py needs for the two tools; `ask_at_start(ctx, svc)` is the
helper `plan_application` and `apply_batch` call BEFORE any planning, so a batch is never interrupted mid-run.
"""
from __future__ import annotations

import json
import weakref
from collections.abc import Callable
from typing import Any

from mcp.server.fastmcp import Context
from pydantic import Field, create_model

from .gaps import SCOPES, ProfileDB, shared
from .guard import blocked_label, is_demographic_key, sensitive_key

_asked = False      # fallback when the context has no session object
_asked_sessions: weakref.WeakSet[Any] = weakref.WeakSet()    # one interactive round per client session


def db_for(settings: Any) -> ProfileDB:
    return shared(settings.paths.data_dir)


def parse_scope(scope: str, scope_value: str = "") -> tuple[str, str]:
    """'global' | 'company' | 'role_family', optionally 'company:Acme' / 'role_family:data'."""
    s, _, v = scope.partition(":")
    s = s.strip().lower().replace("-", "_")
    if s not in SCOPES:
        raise ValueError(f"scope must be one of {', '.join(SCOPES)}")
    return s, (scope_value or v).strip()


def store_answer(settings: Any, key: str, value: str, scope: str = "global", scope_value: str = "",
                 origin: str = "client") -> dict[str, Any]:
    """`origin="user_written"` only from a path where the human typed or confirmed the value (elicitation, TTY CLI).
    A client-origin answer to a sensitive key is kept as a suggestion: it neither settles the gap nor is ever used."""
    s, v = parse_scope(scope, scope_value)
    key = key.strip()
    if not key or not value.strip():
        raise ValueError("key and value are required")
    if is_demographic_key(key):
        raise ValueError("demographic answers come from [demographics] in profile.toml only")
    human = origin == "user_written"
    db_for(settings).set_answer(key, value.strip(), s, v, origin=origin, settle=human or not sensitive_key(key))
    out: dict[str, Any] = {"ok": True, "key": key, "scope": s + (f":{v}" if v else ""), "origin": origin}
    if not human and sensitive_key(key):
        out["note"] = "stored as a suggestion; it is used only after the user confirms it (profile ask)"
    return out


def questions(settings: Any, limit: int) -> list[dict[str, Any]]:
    rows = db_for(settings).questions(limit=limit * 4, include_skipped=False)
    mn = settings.persona.min_hits
    db = db_for(settings)
    out = []
    for r in rows:
        k = r["key"]
        if r["n"] < mn or is_demographic_key(k) or (not k.startswith("persona.") and k.startswith("q:")
                                                     and blocked_label(r["question"])):
            continue
        if (sug := db.suggestion(k)) is not None:
            r["suggested"] = sug[:120]
        out.append(r)
    return out[:max(1, limit)]


async def ask_at_start(ctx: Any, get_svc: Callable[[], Any]) -> int:
    """Ask the top gaps over MCP elicitation, once. Returns the number answered. Silent when the client cannot
    elicit, when disabled, or when nothing is open."""
    global _asked
    sess = getattr(ctx, "session", None)
    try:
        if sess is not None:
            if sess in _asked_sessions:
                return 0
            _asked_sessions.add(sess)
        else:
            if _asked:
                return 0
            _asked = True
    except TypeError:
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
            note = (f" (suggested earlier: {r['suggested']})" if r.get("suggested") else "") + f" (now defaulting to: {r['default_used']})" if r.get("default_used") else ""
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
            db.set_answer(r["key"], val, "global", origin="user_written")      # the human typed it in the form
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
    async def answer_profile_question(ctx: Context, key: str, value: str, scope: str = "global",
                                      scope_value: str = "") -> str:
        """Propose an answer for a gap key (from profile_questions). The USER is asked to confirm or edit it through
        the client (elicitation); only then is it stored as user_written and applied to future plans. Without
        elicitation it is stored as origin=client: used for ordinary questions, but for authorization, sponsorship,
        citizenship, clearance, export, consent and similar keys it stays a suggestion until the user confirms it
        (`job-apply-mcp profile ask`). In approval_mode=human a non-confirmed answer is refused. Demographics are
        never stored here. Scope: global | company | role_family (+ scope_value, or 'company:Acme')."""
        st = get_svc().settings
        try:
            parse_scope(scope, scope_value)
            if is_demographic_key(key.strip()):
                raise ValueError("demographic answers come from [demographics] in profile.toml only")
            confirmed = await _confirm(ctx, key, value, scope, scope_value)
            if confirmed is None:
                if st.apply.approval_mode == "human":
                    raise ValueError("this client cannot ask the user; run `job-apply-mcp profile answer` or `profile ask`")
                out = store_answer(st, key, value, scope, scope_value, origin="client")
            elif confirmed == "":
                out = {"ok": False, "error": "the user declined"}
            else:
                out = store_answer(st, key, confirmed, scope, scope_value, origin="user_written")
        except ValueError as e:
            out = {"ok": False, "error": str(e)}
        return json.dumps(out, separators=(",", ":"))


async def _confirm(ctx: Any, key: str, value: str, scope: str, scope_value: str) -> str | None:
    """The human's confirmed value; "" when declined; None when the client cannot elicit."""
    try:
        res = await ctx.elicit(message=f"An assistant proposes this answer for '{key}' (scope {scope}"
                                       f"{':' + scope_value if scope_value else ''}). It will be reused on future "
                                       "applications. Confirm or edit it.",
                               schema=_confirm_model(value))
    except Exception:  # noqa: BLE001 - client without elicitation
        return None
    return (res.data.value or "").strip() if res.action == "accept" and res.data else ""


def _confirm_model(value: str):
    return create_model("ConfirmAnswer", value=(str, Field(default=value, description="Edit if needed, then accept")))

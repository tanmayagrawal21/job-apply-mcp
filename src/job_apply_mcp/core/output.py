"""Schema-aware output shaping for MCP tools (ADR-0005 amendment).

Guarantees: every result is valid compact JSON, never cut mid-token, and within `budget` chars (best effort
down to a tiny fixed fallback). Shaping order: (1) paginate arrays, (2) truncate long strings with an explicit
marker, (3) drop optional verbose fields per priority list, (4) aggressive pass, (5) minimal fallback.
Envelope: success `{"ok":true,...}`, failure `{"ok":false,"error":{"code","message","hint"}}`.
"""
from __future__ import annotations

import contextvars
import copy
import functools
import json
from collections.abc import Callable
from typing import Any

# Per-tool optional verbose fields, dropped first-to-last (any depth) when still over budget.
DROP_PRIORITY: dict[str, tuple[str, ...]] = {
    "get_job": ("description", "raw", "html"),
    "get_form": ("options", "help", "raw"),
    "plan_application": ("evidence", "debug", "trace"),
    "submit_application": ("evidence", "debug", "trace", "raw"),
    "apply_batch": ("evidence", "debug", "trace", "detail"),
    "search_jobs": ("desc", "description", "raw", "tags"),
}
GENERIC_DROP = ("raw", "html", "debug", "trace", "evidence", "description", "desc", "detail", "text")
_MIN_STR = 60
_tool_name: contextvars.ContextVar[str] = contextvars.ContextVar("tool_name", default="")


def dumps(o: Any) -> str:
    return json.dumps(o, separators=(",", ":"), default=str, ensure_ascii=False)


def err(code: str, message: str, hint: str = "") -> str:
    return dumps({"ok": False, "error": {"code": code, "message": str(message)[:300], "hint": hint}})


_MAP: tuple[tuple[str, str, str], ...] = (
    ("NotImplementedError", "not_implemented", "This capability is not built yet; call health to see what is available."),
    ("ComponentMissing", "component_missing", "Install the needed extra or enable the component in config."),
    ("ValidationError", "invalid_input", "Check argument names and types against the tool description."),
    ("ValueError", "invalid_input", "Check argument values against the tool description."),
    ("TypeError", "invalid_input", "Check argument types against the tool description."),
    ("KeyError", "not_found", "Verify the ref/id exists (use search_jobs or list_applications)."),
    ("ProviderUnavailable", "unavailable", "A backing provider is unreachable; retry later or use another."),
    ("HttpError", "upstream_error", "The remote site failed; retry later."),
    ("TimeoutError", "timeout", "Retry, or lower max_parallel."),
)


def error_from_exc(e: BaseException) -> str:
    names = {c.__name__ for c in type(e).__mro__}
    for name, code, hint in _MAP:
        if name in names:
            msg = e.args[0] if isinstance(e, KeyError) and e.args else e
            return err(code, f"{type(e).__name__}: {msg}", hint)
    return err("internal", f"{type(e).__name__}: {e}", "Unexpected failure; see server logs.")


# ---- shaping -----------------------------------------------------------------------------------------------

def _size(o: Any) -> int:
    return len(dumps(o))


def _lists(o: Any, depth: int = 0, maxdepth: int = 1):
    """Yield (container_dict_or_None, key, list) for arrays at the top level (and, if maxdepth, one deeper)."""
    if isinstance(o, list):
        yield None, None, o
    elif isinstance(o, dict):
        for k, v in o.items():
            if isinstance(v, list):
                yield o, k, v


def _paginate(o: Any, floor: int) -> bool:
    best = max(((len(dumps(v)), c, k, v) for c, k, v in _lists(o) if len(v) > floor), default=None,
               key=lambda t: t[0])
    if best is None:
        return False
    _, c, _k, v = best
    keep = max(floor, len(v) // 2)
    omitted = len(v) - keep
    del v[keep:]
    if c is not None:
        if "omitted" in c:   # already trimmed once: cursor moves back by what was just removed
            c["next"] -= omitted
        else:
            c["next"] = keep
        c["omitted"] = c.get("omitted", 0) + omitted
    return True


def _strings(o: Any):
    stack = [o]
    while stack:
        x = stack.pop()
        if isinstance(x, dict):
            for k, v in x.items():
                if isinstance(v, str):
                    yield x, k, v
                else:
                    stack.append(v)
        elif isinstance(x, list):
            for i, v in enumerate(x):
                if isinstance(v, str):
                    yield x, i, v
                else:
                    stack.append(v)


def _truncate_strings(o: Any, thresh: int) -> bool:
    """Shorten every string longer than `thresh` to about half, ending in an explicit marker."""
    changed = False
    for c, k, v in list(_strings(o)):
        if len(v) > thresh:
            keep = max(thresh // 2, 8)
            new = v[:keep] + f"…[+{len(v) - keep} chars]"
            if len(new) < len(v):
                c[k] = new
                changed = True
    return changed


def _paginate_deep(o: Any, keep: int) -> bool:
    """Trim every nested list to `keep` items (parent dict records next/omitted)."""
    changed = False
    stack = [o]
    while stack:
        x = stack.pop()
        if isinstance(x, dict):
            for k, v in list(x.items()):
                if isinstance(v, list) and len(v) > keep:
                    x["next"] = keep if "omitted" not in x else x["next"] - (len(v) - keep)
                    x["omitted"] = x.get("omitted", 0) + len(v) - keep
                    del v[keep:]
                    changed = True
            stack.extend(x.values())
        elif isinstance(x, list):
            stack.extend(x)
    return changed


def _drop(o: Any, names: tuple[str, ...], dropped: list[str]) -> bool:
    for name in names:
        hit = False
        stack = [o]
        while stack:
            x = stack.pop()
            if isinstance(x, dict):
                if name in x:
                    del x[name]
                    hit = True
                stack.extend(x.values())
            elif isinstance(x, list):
                stack.extend(x)
        if hit:
            dropped.append(name)
            return True
    return False


def shape(obj: Any, budget: int, tool: str = "") -> str:
    """Serialize `obj` to valid compact JSON within `budget` chars, degrading by schema, never by slicing."""
    s = dumps(obj)
    if len(s) <= budget:
        return s
    o = copy.deepcopy(json.loads(s))   # normalise (default=str) and avoid mutating caller data
    if not isinstance(o, (dict, list)):
        o = {"value": o}
    names = tuple(dict.fromkeys(DROP_PRIORITY.get(tool, ()) + GENERIC_DROP))
    dropped: list[str] = []

    def fits() -> bool:
        return _size(o) <= budget

    while not fits() and _paginate(o, 3):          # 1. keep first N items, add next/omitted
        pass
    t = 2000
    while not fits() and t >= _MIN_STR:               # 2. long strings -> marker
        _truncate_strings(o, t)
        t //= 2
    while not fits() and _drop(o, names, dropped):   # 3. optional verbose fields
        pass
    if not fits():                                   # 4. aggressive
        for keep in (3, 1, 0):
            _paginate_deep(o, keep)
            if fits():
                break
        while not fits() and _paginate(o, 0):
            pass
        t = _MIN_STR
        while not fits() and t >= 16:
            _truncate_strings(o, t)
            t //= 2
    if dropped and isinstance(o, dict):
        o["dropped"] = dropped
    if _size(o) > budget:                            # 5. minimal fallback
        keys = list(o)[:8] if isinstance(o, dict) else []
        o = {"ok": True, "truncated": True, "chars": len(s), "keys": keys}
    return dumps(o)


def envelope(obj: Any) -> Any:
    """Ensure success results carry ok:true (compact key); errors already carry ok:false."""
    if isinstance(obj, dict):
        return obj if "ok" in obj else {"ok": True, **obj}
    return {"ok": True, "items": obj} if isinstance(obj, list) else {"ok": True, "value": obj}


def out(obj: Any, budget: int, tool: str | None = None) -> str:
    return shape(envelope(obj), budget, tool if tool is not None else _tool_name.get())


def guarded(fn: Callable[..., Any], budget: Callable[[], int]) -> Callable[..., Any]:
    """Wrap an async tool: exceptions -> error envelope; string results -> envelope + budget shaping."""
    name = fn.__name__

    @functools.wraps(fn)
    async def wrapper(*a: Any, **kw: Any) -> str:
        tok = _tool_name.set(name)
        try:
            res = await fn(*a, **kw)
            if isinstance(res, str):
                try:
                    res = json.loads(res)
                except ValueError:
                    res = {"text": res}
            return out(res, budget(), name)
        except Exception as e:  # noqa: BLE001 - boundary: never leak a traceback/non-JSON to the client
            return error_from_exc(e)
        finally:
            _tool_name.reset(tok)

    return wrapper

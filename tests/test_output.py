import asyncio
import json
import random

import pytest

from job_apply_mcp import server
from job_apply_mcp.core import output


def rnd(rng, depth=0):
    r = rng.random()
    if depth > 2 or r < 0.3:
        return rng.choice([rng.randint(0, 99), None, True, "x" * rng.randint(0, 1500), "é\n\"q\"" * rng.randint(1, 50)])
    if r < 0.65:
        return [rnd(rng, depth + 1) for _ in range(rng.randint(0, 8))]
    return {f"k{i}": rnd(rng, depth + 1) for i in range(rng.randint(0, 8))} | {"description": "d" * rng.randint(0, 1500)}


@pytest.mark.parametrize("budget", [4000, 1500, 400])
def test_random_payloads_always_valid_json_under_budget(budget):
    rng = random.Random(7)
    for n in range(120):
        payload = {"rows": [rnd(rng) for _ in range(1 + n % 30)], "meta": rnd(rng)}
        s = output.out(payload, budget, "search_jobs")
        d = json.loads(s)
        assert len(s) <= budget
        assert d["ok"] is True or d.get("truncated")


def test_pagination_marker_and_string_marker():
    rows = [{"ref": f"r{i}", "co": "Acme", "title": "t" * 20} for i in range(200)]
    d = json.loads(output.out({"rows": rows}, 1000))
    assert d["omitted"] > 0 and d["next"] == len(d["rows"]) and d["rows"][0]["ref"] == "r0"
    d = json.loads(output.out({"description": "word " * 5000}, 500))
    assert "…[+" in d["description"] and "chars]" in d["description"]


def test_priority_drop_and_small_untouched():
    d = json.loads(output.out({"title": "a", "description": "z" * 100}, 5000, "get_job"))
    assert d == {"ok": True, "title": "a", "description": "z" * 100}
    d = json.loads(output.out({"title": "a", "description": "z " * 400, "x": 1}, 90, "get_job"))
    assert "description" not in d or len(d["description"]) < 90


def test_input_not_mutated_and_non_json_types():
    p = {"rows": list(range(500)), "o": object()}
    json.loads(output.out(p, 300))
    assert len(p["rows"]) == 500


@pytest.mark.parametrize("exc,code", [
    (NotImplementedError("nope"), "not_implemented"),
    (type("ComponentMissing", (RuntimeError,), {})("x"), "component_missing"),
    (ValueError("bad"), "invalid_input"),
    (KeyError("k"), "not_found"),
    (RuntimeError("boom"), "internal"),
])
def test_error_mapping(exc, code):
    d = json.loads(output.error_from_exc(exc))
    assert d["ok"] is False and d["error"]["code"] == code and set(d["error"]) == {"code", "message", "hint"}


def test_every_registered_tool_error_path_returns_envelope(monkeypatch):
    class Boom:
        def __getattr__(self, name):
            async def f(*a, **k):
                raise NotImplementedError(name)
            return f

    class S(Boom):
        settings = type("St", (), {"response_budget_chars": 4000})()

    monkeypatch.setattr(server, "_svc", S())
    tools = asyncio.run(server.mcp.list_tools())
    assert len(tools) >= 12
    checked = 0
    for t in tools:
        fn = server.mcp._tool_manager._tools[t.name].fn
        # call with schema-minimal args; unknown extras tools may fail inside their own try -> still JSON
        args = {k: "x" for k in t.inputSchema.get("required", [])}
        res = asyncio.run(fn(**args))
        d = json.loads(res)
        if t.name in {"control_check", "control_status"} or d.get("ok") is True:
            continue  # own control-layer path / handled without services
        assert d["ok"] is False and set(d["error"]) == {"code", "message", "hint"}, t.name
        checked += 1
    assert checked >= 10

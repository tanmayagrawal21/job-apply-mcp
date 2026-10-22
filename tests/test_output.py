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


def _text(res):
    blocks = res[0] if isinstance(res, tuple) else res
    return blocks[0].text


def test_every_tool_bad_args_return_envelope():
    tools = asyncio.run(server.mcp.list_tools())
    n = 0
    for t in tools:
        props = t.inputSchema.get("properties", {})
        required = t.inputSchema.get("required", [])
        if required:
            bad = {}                                   # missing required args
        elif props:
            k, v = next(iter(props.items()))
            bad = {k: {"not": ["a", "valid"]}} if v.get("type") != "object" else {k: 5}
        else:
            continue                                   # no arguments to get wrong
        d = json.loads(_text(asyncio.run(server.mcp.call_tool(t.name, bad))))
        assert d["ok"] is False and d["error"]["code"] == "invalid_args", (t.name, d)
        assert set(d["error"]) == {"code", "message", "hint"}
        n += 1
    assert n >= 15
    d = json.loads(_text(asyncio.run(server.mcp.call_tool("nope", {}))))
    assert d["error"]["code"] == "unknown_tool"


def test_deep_nesting_and_cycles_do_not_recurse():
    deep = cur = {}
    for _ in range(5000):
        cur["a"] = {}
        cur = cur["a"]
    s = output.out({"x": deep}, 4000)
    assert "…[depth]" in s and json.loads(s)["ok"] is True
    cyc: list = []
    cyc.append(cyc)
    json.loads(output.out(cyc, 4000))


def test_traceback_logged_not_returned(caplog):
    async def boom():
        raise RuntimeError("secret-internal-detail")

    boom.__name__ = "boomtool"
    w = output.guarded(boom, lambda: 4000)
    with caplog.at_level("ERROR", logger="job_apply_mcp.tools"):
        res = asyncio.run(w())
    assert "Traceback" not in res and json.loads(res)["error"]["code"] == "internal"
    assert any(r.exc_info for r in caplog.records)


def test_paging_cursor_has_no_duplicates():
    rows = [{"ref": f"r{i}", "pad": "p" * 40} for i in range(100)]

    async def list_x(cursor: int = 0):
        return {"rows": rows[cursor:cursor + 50]}

    w = output.guarded(list_x, lambda: 800)
    seen, cur = [], 0
    for _ in range(50):
        d = json.loads(asyncio.run(w(cursor=cur)))
        seen += [r["ref"] for r in d["rows"]]
        cur = d["next"] if d.get("omitted") else cur + 50
        if cur >= 100:
            break
    assert seen == [f"r{i}" for i in range(100)]


def test_improvement_report_pages(monkeypatch):
    props = [{"id": f"p{i}"} for i in range(25)]

    class S:
        settings = type("St", (), {"response_budget_chars": 4000})()

        async def improvement_report(self, limit):
            return {"proposals": props[:limit], "total": 25}

    monkeypatch.setattr(server, "_svc", S())
    p1 = json.loads(asyncio.run(server.improvement_report(limit=10)))
    p2 = json.loads(asyncio.run(server.improvement_report(limit=10, cursor=p1["next"])))
    ids = [x["id"] for x in p1["proposals"] + p2["proposals"]]
    assert len(ids) == len(set(ids)) == 20 and p2["next"] == 20

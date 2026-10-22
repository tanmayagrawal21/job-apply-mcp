import asyncio
import json

import httpx
import pytest
import respx

from job_apply_mcp.config import LLMCfg, LLMTier, Settings
from job_apply_mcp.core import registry
from job_apply_mcp.llm import LLMRouter, Task
from job_apply_mcp.llm.base import extract_json, validate_schema
from job_apply_mcp.llm.providers.client import current_sampler
from job_apply_mcp.llm.providers.fake import FakeProvider


def mk(fakes: dict, **over):
    cfg = LLMCfg(retries=1, backoff_s=0.0, **over)
    return LLMRouter(cfg, {"default": 4}, providers=fakes)


def all_fake(**kw):
    return {"ollama": FakeProvider("ollama", **kw), "client": FakeProvider("client", is_cloud=True)}


def test_registered():
    assert {"ollama", "openai_compat", "anthropic", "hf_local", "client", "fake", "litellm"} <= set(
        registry.llm_providers.names())


async def test_simple_field_routes_small():
    f = all_fake(responses=["Yes"])
    r = mk(f)
    res = await r.run(Task(prompt="Authorized to work?", field_kind="boolean", question="Authorized?"))
    assert res.ok and res.tier == "small" and res.model == "qwen3:4b" and res.task_class == "field"


async def test_textarea_company_specific_goes_up():
    r = mk(all_fake())
    d = await r.decide(Task(prompt="p", field_kind="textarea", question="Describe a challenge",
                            expected_chars=300, company_specific=True))
    assert d["tier"] == "large" and d["score"] >= 5


async def test_prior_failure_bumps_tier():
    r = mk(all_fake())
    assert (await r.decide(Task(prompt="p", field_kind="text", prior_failures=1)))["tier"] == "medium"


async def test_narrative_class_and_tier():
    f = all_fake(responses=["A letter."])
    r = mk(f)
    res = await r.run(Task(prompt="write", question="Cover letter", field_kind="textarea"))
    assert res.task_class == "narrative" and res.model == "gemma4:12b-it-qat"
    assert f["ollama"].calls[0]["max_tokens"] == 1200
    d = await r.decide(Task(prompt="p", field_kind="textarea", expected_chars=600))
    assert d["task_class"] == "narrative"
    assert (await r.health())["task_classes"] == {"narrative": 1}


async def test_narrative_falls_back_when_model_missing():
    f = {"ollama": FakeProvider("ollama", models=["qwen2.5:7b"], responses=["x"]),
         "client": FakeProvider("client")}
    res = await mk(f).run(Task(prompt="p", task_class="narrative"))
    assert res.ok and res.model == "qwen2.5:7b"


async def test_review_defaults_to_client_task():
    r = mk({"ollama": FakeProvider("ollama"), "client": __import__(
        "job_apply_mcp.llm.providers.client", fromlist=["x"]).ClientProvider()})
    res = await r.review("my draft", key="cover")
    assert res.task_class == "review" and res.client_task and "my draft" in res.client_task.instruction


async def test_client_sampling_used_when_available():
    from job_apply_mcp.llm.providers.client import ClientProvider

    async def sampler(prompt, **kw):
        return "sampled"

    tok = current_sampler.set(sampler)
    try:
        res = await mk({"client": ClientProvider(), "ollama": FakeProvider("ollama")}).run(
            Task(prompt="p", task_class="review"))
    finally:
        current_sampler.reset(tok)
    assert res.text == "sampled" and res.client_task is None


async def test_allow_cloud_false_skips_client():
    f = all_fake(responses=["local"])
    r = mk(f, allow_cloud=False)
    res = await r.run(Task(prompt="p", task_class="review"))
    assert res.ok and res.provider == "ollama" and res.text == "local"


async def test_fallback_when_primary_down():
    f = {"ollama": FakeProvider("ollama", is_up=False), "client": FakeProvider("client", is_cloud=True,
                                                                              responses=["from client"])}
    res = await mk(f).run(Task(prompt="p", field_kind="text"))
    assert res.ok and res.provider == "client"


async def test_retry_with_backoff_then_success():
    n = {"i": 0}

    def resp(prompt, model, k):
        n["i"] += 1
        return "!retry" if n["i"] == 1 else "fine"

    res = await mk(all_fake(responses=resp)).run(Task(prompt="p", field_kind="text"))
    assert res.text == "fine" and n["i"] == 2


async def test_json_repair_then_ok():
    schema = {"type": "object", "required": ["a"], "properties": {"a": {"type": "integer"}}}
    f = all_fake(responses=["not json", '{"a": 1}'])
    res = await mk(f).run(Task(prompt="p", field_kind="text", json_schema=schema))
    assert res.ok and res.data == {"a": 1} and res.escalations == 0
    assert "invalid" in f["ollama"].calls[1]["prompt"]


async def test_json_failure_escalates_one_tier():
    schema = {"type": "object", "required": ["a"]}

    def resp(prompt, model, k):
        return '{"a": 1}' if model == "qwen2.5:7b" else "garbage"

    res = await mk(all_fake(responses=resp)).run(Task(prompt="p", field_kind="text", json_schema=schema))
    assert res.ok and res.tier == "medium" and res.escalations == 1


async def test_exhausted_returns_error():
    f = all_fake(responses=["bad"])
    res = await mk(f, allow_cloud=False).run(Task(prompt="p", field_kind="text",
                                                    validator=lambda t: "always bad"))
    assert not res.ok and "always bad" in res.error


async def test_per_provider_semaphore_and_heavy_serialization():
    f = all_fake(latency_s=0.02)
    r = LLMRouter(LLMCfg(retries=0), {"ollama": 2, "default": 4}, providers=f)
    await asyncio.gather(*(r.run(Task(prompt="p", field_kind="text")) for _ in range(6)))
    assert f["ollama"].peak <= 2
    f2 = all_fake(latency_s=0.02)
    r2 = LLMRouter(LLMCfg(retries=0), {"ollama": 4}, providers=f2)
    await asyncio.gather(*(r2.run(Task(prompt="p", task_class="narrative")) for _ in range(4)))
    assert f2["ollama"].peak == 1  # heavy default model: one resident at a time


async def test_stats_in_health():
    r = mk(all_fake(responses=["hello"]))
    await r.run(Task(prompt="hello there", field_kind="text"))
    h = await r.health()
    st = h["stats"]["ollama:qwen3:4b"]
    assert st["calls"] == 1 and st["tok_out"] > 0
    assert h["providers"]["ollama"]["up"] and "narrative" in h["task_tiers"]


async def test_services_health_wiring():
    from job_apply_mcp.services import Services

    s = Services(Settings())
    s.llm._providers.update(all_fake())
    h = await s.health()
    assert "llm_router" in h and h["llm_router"]["providers"]["ollama"]["up"]


def test_schema_helpers():
    assert extract_json('```json\n{"a":1}\n```') == {"a": 1}
    assert validate_schema({"a": "x"}, {"properties": {"a": {"type": "integer"}}}) is not None
    assert validate_schema(True, {"type": "integer"}) is not None


@respx.mock
async def test_ollama_provider_http():
    from job_apply_mcp.llm.providers.ollama import OllamaProvider

    respx.get("http://o:1/api/tags").respond(json={"models": [{"name": "m:1"}]})
    route = respx.post("http://o:1/api/chat").respond(json={
        "message": {"content": "<think>x</think>hi"}, "prompt_eval_count": 3, "eval_count": 2})
    p = OllamaProvider(base_url="http://o:1", keep_alive="10m")
    assert await p.available() and await p.has_model("m:1") and not await p.has_model("z")
    c = await p.complete_ex("q", model="m:1", json_schema={"type": "object"})
    body = json.loads(route.calls[0].request.content)
    assert c.text == "hi" and c.tokens_out == 2 and body["format"] == {"type": "object"}
    assert body["keep_alive"] == "10m" and body["stream"] is False


@respx.mock
async def test_ollama_down():
    from job_apply_mcp.llm.providers.ollama import OllamaProvider

    respx.get("http://o:1/api/tags").mock(side_effect=httpx.ConnectError("no"))
    assert not await OllamaProvider(base_url="http://o:1").available()


@respx.mock
async def test_openai_compat_and_key_from_env(monkeypatch):
    from job_apply_mcp.llm.providers.openai_compat import OpenAICompatProvider

    monkeypatch.setenv("MY_KEY", "sk-test")
    route = respx.post("http://127.0.0.1:8080/v1/chat/completions").respond(json={
        "choices": [{"message": {"content": "yo"}}], "usage": {"prompt_tokens": 4, "completion_tokens": 1}})
    p = OpenAICompatProvider(base_url="http://127.0.0.1:8080/v1/", api_key_env="MY_KEY")
    assert not p.is_cloud
    c = await p.complete_ex("q", model="m")
    assert c.text == "yo" and route.calls[0].request.headers["authorization"] == "Bearer sk-test"
    assert OpenAICompatProvider(base_url="https://api.openai.com/v1").is_cloud


@respx.mock
async def test_openai_compat_schema_fallback():
    from job_apply_mcp.llm.providers.openai_compat import OpenAICompatProvider

    route = respx.post("http://h/v1/chat/completions").mock(side_effect=[
        httpx.Response(400, text="no response_format"),
        httpx.Response(200, json={"choices": [{"message": {"content": "{}"}}]})])
    c = await OpenAICompatProvider(base_url="http://h/v1").complete_ex("q", model="m", json_schema={"type": "object"})
    assert c.text == "{}" and route.call_count == 2


@respx.mock
async def test_anthropic_messages(monkeypatch):
    from job_apply_mcp.llm.providers.anthropic import AnthropicProvider

    p = AnthropicProvider()
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    assert not await p.available()
    monkeypatch.setenv("ANTHROPIC_API_KEY", "k")
    route = respx.post("https://api.anthropic.com/v1/messages").respond(json={
        "content": [{"type": "text", "text": "hey"}], "usage": {"input_tokens": 5, "output_tokens": 2}})
    c = await p.complete_ex("q", model="claude-x", system="s")
    assert c.text == "hey" and route.calls[0].request.headers["x-api-key"] == "k"
    assert await p.available()


@respx.mock
async def test_http_429_is_retryable():
    from job_apply_mcp.llm.base import ProviderError
    from job_apply_mcp.llm.providers.openai_compat import OpenAICompatProvider

    respx.post("http://h/chat/completions").respond(429)
    with pytest.raises(ProviderError) as ei:
        await OpenAICompatProvider(base_url="http://h").complete_ex("q", model="m")
    assert ei.value.retryable


async def test_litellm_and_hf_unavailable_by_default():
    from job_apply_mcp.llm.providers.hf_local import HFLocalProvider
    from job_apply_mcp.llm.providers.litellm import LiteLLMProvider

    assert isinstance(await LiteLLMProvider().available(), bool)
    assert isinstance(await HFLocalProvider().available(), bool)


@pytest.mark.live
async def test_live_ollama():
    from job_apply_mcp.llm.providers.ollama import OllamaProvider

    p = OllamaProvider()
    if not await p.available():
        pytest.skip("no local Ollama")
    models = await p.list_models()
    model = next((m for m in ("qwen3:4b", "qwen2.5:7b") if m in models), None)
    if not model:
        pytest.skip("no qwen model")
    r = LLMRouter(LLMCfg(tiers={"small": LLMTier(provider="ollama", model=model, max_tokens=64)},
                         task_tiers={}, fallbacks={}), {"default": 1})
    res = await r.run(Task(prompt='Reply with JSON {"ok": true}', field_kind="text",
                           json_schema={"type": "object", "required": ["ok"]}))
    assert res.ok and res.data["ok"] is True

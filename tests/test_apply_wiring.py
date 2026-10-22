import pytest
from apply_fakes import (
    POSTING,
    FakeAdapter,
    FakeDriver,
    FakeGates,
    FakeProfile,
    FakeRouter,
    FakeStore,
    settings,
)

from job_apply_mcp.apply.factory import build_engine
from job_apply_mcp.apply.ports import ComponentMissing, ControlLike, GatesLike, ProfileLike, RouterLike
from job_apply_mcp.core import registry
from job_apply_mcp.services import Services


def test_fakes_satisfy_protocols():
    assert isinstance(FakeProfile(), ProfileLike) and isinstance(FakeRouter(), RouterLike)
    assert isinstance(FakeGates(), GatesLike)

    class C:
        async def check(self, a, c): ...
    assert isinstance(C(), ControlLike)


def test_driver_modules_self_register():
    import job_apply_mcp.apply.browser_playwright
    import job_apply_mcp.apply.browser_use_driver  # noqa: F401
    assert {"playwright", "browser_use"} <= set(registry.browser_drivers.names())


def test_factory_reports_missing_component_clearly():
    s = settings()
    s.paths.profile = "/nonexistent/profile.toml"
    with pytest.raises(ComponentMissing):
        build_engine(s, store=FakeStore(), gates=FakeGates(), resolve_adapter=lambda r: FakeAdapter())


class _Allow:
    async def check(self, action, ctx):
        return {"decision": "pass"}


async def test_services_wiring_with_injected_engine():
    e = build_engine(settings(apply__auto_approve=True, writing__max_rounds=1), store=FakeStore(),
                     profile=FakeProfile(), gates=FakeGates(), router=FakeRouter(short={}, prose="I am passionate."),
                     resolve_adapter=lambda r: FakeAdapter(), driver_factory=lambda n: FakeDriver(), control=_Allow())
    svc = Services(settings(), engine=e)
    plan = await svc.plan(POSTING.ref)
    assert plan["gate"] == "submit" and plan["token"] and "client_tasks" in plan
    res = await svc.submit(POSTING.ref, {"q_why": "t", "q_source": "Other", "q_tool": "make"}, False, token=plan["token"])
    assert res["status"] == "submitted"
    batch = await svc.apply_batch([POSTING.ref], True, None)
    assert batch["results"][0]["ref"] == POSTING.ref

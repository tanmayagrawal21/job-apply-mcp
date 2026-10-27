"""Build an ApplyEngine from settings, importing the sibling components lazily (clear errors when absent)."""
from __future__ import annotations

import importlib
from typing import Any

from ..config import Settings, state_path
from ..core import registry
from ..core.interfaces import PlatformAdapter
from .engine import ApplyEngine
from .ports import ComponentMissing


def _need(module: str, component: str, branch: str):
    try:
        return importlib.import_module(f"job_apply_mcp.{module}")
    except ImportError as e:
        raise ComponentMissing(f"{component} is not available ({e}); it lands in {branch}") from e


def adapter_resolver(settings: Settings):
    cache: dict[str, PlatformAdapter] = {}
    _need("platforms", "platform adapters", "feat/platform-adapters")

    def get(name: str) -> PlatformAdapter:
        if name not in cache:
            cache[name] = registry.platforms.create(name)
        return cache[name]

    def resolve(ref_or_url: str) -> PlatformAdapter:
        if not ref_or_url.startswith(("http://", "https://")):
            return get(ref_or_url.split(":", 1)[0])
        for name in registry.platforms.names():
            a = get(name)
            if name != "generic" and a.matches(ref_or_url):
                return a
        if "generic" in registry.platforms.names():
            return get("generic")
        raise ComponentMissing(f"no platform adapter matches {ref_or_url}")
    return resolve


def build_engine(settings: Settings, *, store: Any = None, profile: Any = None, router: Any = None,
                 gates: Any = None, resolve_adapter: Any = None, control: Any = None,
                 driver_factory: Any = None) -> ApplyEngine:
    """Every collaborator can be injected (tests, embedding); the rest are created from settings."""
    from . import adapters as ad
    if profile is None:
        prof_mod = _need("profile", "candidate profile", "feat/store-policy")
        try:
            profile = ad.ProfileView(prof_mod.load_profile(settings.paths.profile), settings)
        except FileNotFoundError as e:
            raise ComponentMissing(str(e)) from e
    if gates is None:
        _need("policy.gates", "policy gates", "feat/store-policy")
        gates = ad.PolicyGates(settings)
    if store is None:
        sqlite = _need("store.sqlite", "application store", "feat/store-policy")
        store = sqlite.SqliteStore(state_path(settings), dedupe_days=settings.policy.dedupe_days,
                                  dedupe_mode=settings.dedupe.mode, company_aliases=settings.dedupe.company_aliases,
                                  title_threshold=settings.dedupe.title_threshold)
    if router is None:
        try:
            llm = importlib.import_module("job_apply_mcp.llm.router")
            router = ad.RouterView(llm.LLMRouter(settings))
        except ImportError:
            router = None  # engine degrades to client_tasks for everything it cannot map deterministically
    if control is None:
        try:  # control layer (ADR-0014): optional; fails closed on its own bad config
            control = importlib.import_module("job_apply_mcp.control").ControlAgent(settings)
        except ImportError:
            control = None
    from .approvals import ApprovalBook
    book = ApprovalBook(state_path(settings), ttl_s=settings.apply.approval_ttl_s,
                       reconcile_delays_min=settings.ingest.reconcile_delays_min)
    engine = ApplyEngine(settings, approvals=book, resolve_adapter=resolve_adapter or adapter_resolver(settings),
                         profile=profile, router=router, gates=gates, store=store, driver_factory=driver_factory,
                         control=control)
    wire_persona(engine, settings)
    wire_journal(engine, settings)
    return engine


def wire_journal(engine: ApplyEngine, settings: Settings) -> None:
    """Attach the candidate journal (ADR-0064). It is advisory: a state dir that cannot be resolved leaves it off."""
    from ..journal import CandidateJournal
    try:
        engine.journal = CandidateJournal(state_path(settings))
    except Exception:  # noqa: BLE001
        engine.journal = None


def wire_persona(engine: ApplyEngine, settings: Settings) -> None:
    """Attach the persona policy and the gap collector (ADR-0047). Skipped for fake profiles and when disabled."""
    inner = getattr(engine.profile, "p", None)
    if not settings.persona.enabled or inner is None or not hasattr(inner, "persona"):
        return
    from ..persona import PersonaPolicy
    from ..persona.gaps import shared
    db = shared(settings.paths.data_dir)
    pol = PersonaPolicy(inner.persona, db, getattr(engine.profile, "resume_projects", None))
    engine.persona, engine.gaps = pol, db
    engine.profile.persona = pol

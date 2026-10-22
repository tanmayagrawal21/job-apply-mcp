"""CODER backends. Each builds the command that turns a proposal into working-tree changes.

Backends only edit files inside a prepared worktree; branching, tests, commit, push and `gh pr create`
are done by service.py for every backend, so the safety rails cannot be skipped by a backend.
"""
from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass

from ..config import ImproveCfg, Settings
from .proposal import FeatureProposal


@dataclass
class CoderCall:
    argv: list[str]
    env: dict[str, str] | None = None


def build_prompt(p: FeatureProposal, cfg: ImproveCfg) -> str:
    ctx = p.context
    tmpl = ctx.get("template") or {}
    return "\n".join([
        f"You are improving the job-apply-mcp repository. Implement proposal {p.id} ({p.kind}, risk {p.risk}).",
        "", p.spec, "", "Files to touch (guide): " + ", ".join(p.files),
        "Test plan: " + " | ".join(p.test_plan),
        "Protocols: " + ", ".join(ctx.get("protocols", [])),
        f"Template to imitate: {tmpl.get('path', 'n/a')}", "Related ADRs: " + "; ".join(ctx.get("adrs", [])),
        "", "Rules: only edit under " + ", ".join(cfg.allowed_paths) + "; never touch " + ", ".join(cfg.forbidden_paths)
        + "; never read or write secrets or personal data; add offline tests; add/extend an ADR; "
        "do not run git commit/push or gh (the harness does it); do not change dependencies.",
        "Full proposal is in the proposal.md path given by the harness; keep your final message under 100 words."])


Backend = Callable[[FeatureProposal, ImproveCfg, Settings], CoderCall | None]
BACKENDS: dict[str, Backend] = {}


def backend(name: str):
    def deco(fn: Backend) -> Backend:
        BACKENDS[name] = fn
        return fn
    return deco


@backend("claude_code")
def claude_code(p: FeatureProposal, cfg: ImproveCfg, s: Settings) -> CoderCall:
    return CoderCall([cfg.claude_cmd, "-p", build_prompt(p, cfg), "--allowedTools", ",".join(cfg.claude_allowed_tools),
                      "--max-turns", str(cfg.claude_max_turns), "--permission-mode", "acceptEdits"])


def _plain_allowed(path: str, cfg: ImproveCfg) -> bool:
    from .service import path_ok

    return path.endswith(".py") and "*" not in path and "NNNN" not in path and path_ok(
        path, cfg.allowed_paths, cfg.forbidden_paths)


_AIDER_PREFIX = {"ollama": "ollama_chat", "openai_compat": "openai", "anthropic": "anthropic"}
_AIDER_BASE_ENV = {"ollama": "OLLAMA_API_BASE", "openai_compat": "OPENAI_API_BASE"}


def resolve_aider_model(cfg: ImproveCfg, s: Settings) -> tuple[str, dict[str, str]]:
    """A tier name resolves through the router's provider config; anything else is passed to aider as is."""
    tier = s.llm.tiers.get(cfg.aider_model)
    if not tier:
        return cfg.aider_model, {}
    env: dict[str, str] = {}
    base = s.llm.providers.get(tier.provider, {}).get("base_url")
    if base and tier.provider in _AIDER_BASE_ENV:
        env[_AIDER_BASE_ENV[tier.provider]] = base
    return f"{_AIDER_PREFIX.get(tier.provider, tier.provider)}/{tier.model}", env


@backend("aider")
def aider(p: FeatureProposal, cfg: ImproveCfg, s: Settings) -> CoderCall:
    model, env = resolve_aider_model(cfg, s)
    # --yes-always is required headless; scope is bounded by an explicit --file allowlist (existing, allowed
    # paths only) and the harness re-checks every changed path. aider never commits: the harness does.
    files = [f for f in p.files if _plain_allowed(f, cfg)]
    argv = [cfg.aider_cmd, "--yes-always", "--no-auto-commits", "--no-gitignore", "--model", model]
    for f in files:
        argv += ["--file", f]
    return CoderCall(argv + ["--message", build_prompt(p, cfg)], env or None)


@backend("none")
def none(p, cfg, s) -> None:
    return None


@backend("client")
def client(p, cfg, s) -> None:
    return None

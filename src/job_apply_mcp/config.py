"""Layered configuration (ADR-0003).

Precedence, lowest to highest:
  1. built-in defaults (this file)
  2. user file   ~/.config/job-apply-mcp/config.toml   (or $JOB_APPLY_MCP_CONFIG)
  3. project file ./job-apply-mcp.toml
  4. environment variables  JOB_APPLY__SECTION__KEY=value   (double underscore = nesting)
  5. per-call tool arguments (where a tool exposes them)

Secrets (API keys) are only ever read from the environment or a .env file next to the user config,
never from the project file, so a repo checkout cannot leak them.
"""
from __future__ import annotations

import os
import tomllib
from pathlib import Path
from typing import Any

from pydantic import BaseModel, Field


class ConcurrencyCfg(BaseModel):
    global_limit: int = 16          # max concurrent outbound tasks
    per_host: int = 4               # max concurrent requests per host
    min_interval_s: float = 0.25    # polite spacing per host
    browser_workers: int = 2        # concurrent browser sessions
    llm_parallel: dict[str, int] = Field(default_factory=lambda: {"ollama": 1, "default": 4})


class LLMTier(BaseModel):
    provider: str
    model: str
    max_tokens: int = 512


class LLMCfg(BaseModel):
    # Ordered tiers; the router picks the cheapest tier whose capability >= task complexity.
    tiers: dict[str, LLMTier] = Field(default_factory=lambda: {
        "small": LLMTier(provider="ollama", model="qwen3:4b", max_tokens=256),
        "medium": LLMTier(provider="ollama", model="qwen2.5:7b", max_tokens=768),
        "large": LLMTier(provider="client", model="client-default", max_tokens=1500),
    })
    allow_cloud: bool = True        # if False, 'large' falls back to the biggest local tier
    providers: dict[str, dict[str, Any]] = Field(default_factory=lambda: {
        "ollama": {"base_url": "http://127.0.0.1:11434"},
        "openai_compat": {"base_url": "", "api_key_env": "OPENAI_API_KEY"},
        "anthropic": {"api_key_env": "ANTHROPIC_API_KEY"},
        "hf_local": {"device": "mps"},
    })


class PolicyCfg(BaseModel):
    max_years_required: int | None = None
    salary_floor: int | None = None              # annual, in the candidate's home market
    col_multipliers: dict[str, float] = Field(default_factory=dict)  # city substring -> multiplier
    requires_sponsorship: bool | None = None
    graduation: str | None = None                # e.g. "2026-05"
    exclude_employer_patterns: list[str] = Field(default_factory=list)
    stop_phrases: list[str] = Field(default_factory=lambda: [
        "personally completed", "my own work", "without the use of ai", "no ai", "ai policy",
    ])
    dedupe_days: int = 180


class PathsCfg(BaseModel):
    data_dir: str = "~/.local/share/job-apply-mcp"
    profile: str = "~/.config/job-apply-mcp/profile.toml"
    resume: str | None = None


class ApplyCfg(BaseModel):
    auto_approve: bool = False          # True skips the hash-bound approval token (ADR-0011)
    short_text_max: int = 200           # unresolved fields up to this length go to the local LLM
    llm_min_confidence: float = 0.6     # below this an LLM answer is treated as unresolved
    plan_ttl_s: int = 1800              # how long a plan (and its token) stays valid
    context_chars: int = 900            # cap on the posting summary inside client_tasks
    plan_workers: int = 4               # concurrent planners; separate from browser_workers (pipelining)
    write_optional: bool = False        # also write optional long free-text fields (default: skip them)


class BrowserCfg(BaseModel):
    driver: str = "playwright"
    fallback_driver: str | None = "browser_use"   # used for sites the primary driver does not know
    user_data_dir: str = "~/.local/share/job-apply-mcp/browser-profile"
    headless: bool = False
    keep_pages_open: bool = False       # debugging: leave the job's tab open
    channel: str | None = None          # e.g. "chrome" to use the installed Chrome
    timeout_s: float = 30.0
    upload_settle_s: float = 4.0        # wait after a resume upload (parsers re-render the form)
    confirmation_wait_s: float = 10.0
    agent_max_steps: int = 40
    agent_tier: str = "medium"          # llm tier whose model drives the generic agent
    block_resources: list[str] = Field(default_factory=lambda: ["image", "font", "media"])
    block_hosts: list[str] = Field(default_factory=lambda: [
        "google-analytics.com", "googletagmanager.com", "segment.io", "segment.com", "hotjar.com",
        "doubleclick.net", "facebook.net", "fullstory.com", "mixpanel.com", "sentry.io"])
    agent_dry_run_fill: bool = False    # the generic agent cannot be guaranteed to stop before submit


class WritingCfg(BaseModel):
    enabled: bool = True
    style_exemplar: str | None = None   # path outside the repo; few-shot by structure and tone only
    paragraphs_min: int = 4
    paragraphs_max: int = 6
    max_rounds: int = 3
    reviewer: str = "client"            # "client" = review is a client_task; "router" = task_class="review"
    rules: list[str] = Field(default_factory=lambda: [
        "Formal register; no contractions.",
        "Every factual claim must map to a provided profile fact.",
        "Never state gaps, missing skills, or what the candidate lacks.",
        "Never claim feelings or motivations (excited, passionate, drawn to) and never flatter the company.",
        "Connect the company's stated problem to concrete profile facts.",
    ])
    banned_phrases: list[str] = Field(default_factory=lambda: [
        "excited", "passionate", "thrilled", "delighted", "love to", "drew me", "drawn to", "what excites",
        "dream job", "admire", "world-class", "cutting-edge", "innovative company", "leading company",
        "i am eager", "i'm eager", "honored", "humbled",
    ])
    gap_patterns: list[str] = Field(default_factory=lambda: [
        r"\bI have not\b", r"\bI haven't\b", r"\bI lack\b", r"\bI would need to learn\b",
        r"\bI do not have (?:experience|a background)\b", r"\blimited experience\b", r"\bI am still learning\b",
        r"\bnew to\b", r"\bdespite (?:my )?(?:lack|limited)\b",
    ])


class Settings(BaseModel):
    concurrency: ConcurrencyCfg = Field(default_factory=ConcurrencyCfg)
    llm: LLMCfg = Field(default_factory=LLMCfg)
    policy: PolicyCfg = Field(default_factory=PolicyCfg)
    paths: PathsCfg = Field(default_factory=PathsCfg)
    apply: ApplyCfg = Field(default_factory=ApplyCfg)
    browser: BrowserCfg = Field(default_factory=BrowserCfg)
    writing: WritingCfg = Field(default_factory=WritingCfg)
    platforms_enabled: list[str] = Field(default_factory=lambda: ["greenhouse", "lever", "ashby",
                                                                 "smartrecruiters", "workday", "generic"])
    dry_run: bool = True            # safe default: nothing is submitted until the user flips this
    response_budget_chars: int = 4000   # tools truncate/paginate to stay under this


def _deep_merge(a: dict, b: dict) -> dict:
    out = dict(a)
    for k, v in b.items():
        out[k] = _deep_merge(out[k], v) if isinstance(v, dict) and isinstance(out.get(k), dict) else v
    return out


def _env_overrides(prefix: str = "JOB_APPLY__") -> dict:
    out: dict = {}
    for k, v in os.environ.items():
        if not k.startswith(prefix):
            continue
        parts = k[len(prefix):].lower().split("__")
        cur = out
        for p in parts[:-1]:
            cur = cur.setdefault(p, {})
        cur[parts[-1]] = _coerce(v)
    return out


def _coerce(v: str) -> Any:
    if v.lower() in ("true", "false"):
        return v.lower() == "true"
    for t in (int, float):
        try:
            return t(v)
        except ValueError:
            pass
    return v


def load_settings(project_dir: Path | None = None) -> Settings:
    data: dict = {}
    user = Path(os.environ.get("JOB_APPLY_MCP_CONFIG", "~/.config/job-apply-mcp/config.toml")).expanduser()
    for p in (user, (project_dir or Path.cwd()) / "job-apply-mcp.toml"):
        if p.is_file():
            data = _deep_merge(data, tomllib.loads(p.read_text()))
    data = _deep_merge(data, _env_overrides())
    return Settings.model_validate(data)

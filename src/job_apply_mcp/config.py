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
    # Gate pipeline (ADR-0009). None = run every built-in gate in default order; a list enables/reorders.
    gates: list[str] | None = None
    us_citizen: bool = False                     # candidate facts used by the citizenship gate
    us_person: bool = False                      # citizen or permanent resident (ITAR "US person")
    has_clearance: bool = False
    graduation_flex_months: int = 0              # tolerance around a posting's graduation window
    hours_per_year: int = 2080                   # hourly -> annual conversion


BUILTIN_CONTROLS = ["mode", "quota", "attestation", "hard_stop", "single_submit", "prose_lint",
                    "recipient_deny", "page_text_injection"]


class ControlCfg(BaseModel):
    """Control layer (ADR-0014). Per-control knobs live in `params[<control id>]`; end users add their own
    declarative rules in a TOML file outside the repo (`rules_file`)."""

    enabled: list[str] = Field(default_factory=lambda: list(BUILTIN_CONTROLS))
    rules_file: str | None = None
    params: dict[str, dict[str, Any]] = Field(default_factory=dict)
    apply_enabled: bool = True          # False blocks every side-effecting action
    dev_mode: bool = False              # True forces dry-run for everything
    audit: bool = True
    review_enabled: bool = False        # optional model-based review (router task_class "review")
    review_actions: list[str] = Field(default_factory=lambda: ["write_prose", "send_message"])
    review_fail: str = "warn"           # what to do when the reviewer errors: warn | needs_human


class PathsCfg(BaseModel):
    data_dir: str = "~/.local/share/job-apply-mcp"
    profile: str = "~/.config/job-apply-mcp/profile.toml"
    resume: str | None = None


class PlatformsCfg(BaseModel):
    """Platform adapter knobs (ADR-0007). API keys are never here: they come from the environment as
    <api_key_env_prefix>_<PLATFORM>_<BOARD> (e.g. JOB_APPLY_KEY_GREENHOUSE_ACMECO)."""
    cache_ttl_s: int = 900                  # on-disk HTTP cache freshness; stale entries are revalidated by ETag
    cache_prune_days: int = 7               # cache files untouched for this long are deleted at startup
    timeout_s: float = 20.0
    retries: int = 2                        # idempotent requests only; form submissions are never retried
    backoff_s: float = 0.5
    user_agent: str = "job-apply-mcp/0.1 (+https://github.com/tanmayagrawal21/job-apply-mcp)"
    boards: dict[str, list[str]] = Field(default_factory=dict)   # platform -> boards searched by default
    list_with_descriptions: bool = True     # ask list endpoints for content so text flags work (Greenhouse)
    workday_max_pages: int = 10             # 20 postings per page
    jobspy_sites: list[str] = Field(default_factory=lambda: ["linkedin", "indeed"])
    jobspy_results: int = 50
    api_key_env_prefix: str = "JOB_APPLY_KEY"
    max_description_chars: int = 6000       # get_job trims descriptions to this


class TuneCfg(BaseModel):
    """Local style tuning (ADR-0017). Everything runs on the user's machine; data and adapters live under
    `<paths.data_dir>/tuning/` (outside the repo). Needs the optional extra: `pip install job-apply-mcp[tune]`."""
    backend: str = "mlx"                          # Trainer backend id (tuning.trainer.trainers registry)
    base_model: str = "mlx-community/Qwen3-4B-4bit"   # fits 16 GB; swap for a 4-bit Gemma/Qwen up to ~12B
    kinds: list[str] = Field(default_factory=lambda: ["cover_letter", "answer"])   # provenance kinds trained on
    facts_kind: str = "profile_fact"              # kind whose texts are compacted into the instruction
    facts_chars: int = 600
    context_chars: int = 800
    min_words: int = 20                           # length filters on the user's output text
    max_words: int = 700
    eval_fraction: float = 0.2
    paragraph_split: bool = False                 # tiny corpora: one example per paragraph (weak, leaks style)
    seed: int = 7
    # training budget (small enough for a 16 GB laptop)
    iters: int = 200
    batch_size: int = 1
    num_layers: int = 8
    learning_rate: float = 1e-5
    max_seq_length: int = 1536
    grad_checkpoint: bool = True
    time_budget_s: int = 1500                     # hard wall-clock cap; the last checkpoint is kept
    save_every: int = 50
    # when to run
    new_data_threshold: int = 10                  # new user texts since the last run that trigger "new_data"
    auto_run: bool = False                        # tune_status() may start a due run in the background
    allow_on_battery: bool = False
    allow_with_heavy_model: bool = False          # another model (e.g. in Ollama) resident in memory
    # eval gate
    min_eval_examples: int = 2
    max_gen_tokens: int = 400
    gate_min_nll_gain: float = 0.0                # relative improvement in held-out loss required (0.02 = 2%)
    gate_max_lint_regress: float = 0.0            # allowed increase in lint violations per 100 words
    gate_max_length_regress: float = 0.05         # allowed increase in mean |len ratio - 1|
    use_judge: bool = False                       # optional review-tier judge (see tuning.evalgate.set_judge)
    keep_runs: int = 3                            # adapters kept on disk besides promoted/previous
    ollama_base: str | None = None                # Ollama tag matching the adapter's base, for export
    export_create: bool = False                   # also run `ollama create` after writing the Modelfile


class Settings(BaseModel):
    concurrency: ConcurrencyCfg = Field(default_factory=ConcurrencyCfg)
    llm: LLMCfg = Field(default_factory=LLMCfg)
    policy: PolicyCfg = Field(default_factory=PolicyCfg)
    paths: PathsCfg = Field(default_factory=PathsCfg)
    platforms: PlatformsCfg = Field(default_factory=PlatformsCfg)
    control: ControlCfg = Field(default_factory=ControlCfg)
    tune: TuneCfg = Field(default_factory=TuneCfg)
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

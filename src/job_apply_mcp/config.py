"""Layered configuration (ADR-0003).

Precedence, lowest to highest:
  1. built-in defaults (this file)
  2. user file   <config dir>/config.toml (see paths.py; or $JOB_APPLY_MCP_CONFIG)
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

from . import paths


class ConcurrencyCfg(BaseModel):
    global_limit: int = 16          # max concurrent outbound tasks
    per_host: int = 4               # max concurrent requests per host
    min_interval_s: float = 0.25    # polite spacing per host
    browser_workers: int = 2        # concurrent browser sessions
    # keys: "provider" or "provider:model" (most specific wins). Match OLLAMA_NUM_PARALLEL for ollama.
    llm_parallel: dict[str, int] = Field(default_factory=lambda: {"ollama": 2, "default": 4})


class LLMTier(BaseModel):
    provider: str
    model: str
    max_tokens: int = 512
    temperature: float | None = None   # None -> the task's own temperature
    heavy: bool = False                # large local model: calls are serialized (llm.heavy_parallel)
    num_ctx: int | None = None         # Ollama context window; small = faster, less RAM, KV reuse
    think: bool = False                # reasoning mode for models that support it (qwen3); off = fast
    timeout_s: float | None = None     # latency budget for this tier (None -> llm.timeout_s)


class RoutingCfg(BaseModel):
    """Cheap rule-based complexity scoring (ADR-0008). Points add up; thresholds map score -> tier."""
    thresholds: dict[str, int] = Field(default_factory=lambda: {"medium": 2, "large": 5})
    kind_points: dict[str, int] = Field(default_factory=lambda: {
        "text": 0, "select": 0, "boolean": 0, "number": 0, "date": 0, "location": 0, "file": 0,
        "multiselect": 1, "unknown": 1, "textarea": 2})
    length_points: list[tuple[int, int]] = Field(default_factory=lambda: [(300, 1), (1200, 2)])  # (chars, pts)
    essay_keywords: list[str] = Field(default_factory=lambda: [
        "why", "describe", "tell us", "explain", "passion", "motivat", "interest you", "challenge",
        "proud", "cover letter", "essay", "in your own words"])
    essay_points: int = 2
    company_points: int = 2
    failure_points: int = 2            # per prior validation failure
    self_check: bool = False           # ask the small model to rate its confidence before committing
    self_check_min: int = 6            # confidence 0-10 below this adds confidence_points
    confidence_points: int = 2
    batch_enabled: bool = True         # one LLM call per form for all short unresolved fields
    batch_max_fields: int = 12
    batch_max_expected_chars: int = 200   # fields expecting more are answered individually
    narrative_min_words: int = 60      # prose at least this long is class "narrative"
    narrative_keywords: list[str] = Field(default_factory=lambda: [
        "cover letter", "why us", "why do you want", "why are you interested", "tell us about yourself",
        "motivation", "personal statement", "additional information"])


class LLMCfg(BaseModel):
    # Ordered tiers for ordinary fields; the router picks the cheapest tier whose capability >= complexity.
    tiers: dict[str, LLMTier] = Field(default_factory=lambda: {
        # One small model stays resident for all short fields. "medium" is the SAME model with reasoning on,
        # so escalating small -> medium never swaps weights on a 16 GB machine.
        "small": LLMTier(provider="ollama", model="qwen3:4b", max_tokens=256, num_ctx=4096, timeout_s=25),
        "medium": LLMTier(provider="ollama", model="qwen3:4b", max_tokens=1024, num_ctx=4096, think=True,
                          timeout_s=60),
        "large": LLMTier(provider="client", model="client-default", max_tokens=1500),
    })
    # Task-class tiers, chosen by class before complexity. "narrative" = cover letters / why-us / prose;
    # "review" = a second reader that critiques a narrative (default: the calling agent).
    task_tiers: dict[str, LLMTier] = Field(default_factory=lambda: {
        "narrative": LLMTier(provider="ollama", model="gemma4:12b-it-qat", max_tokens=1200,
                             temperature=0.6, heavy=True, num_ctx=8192, timeout_s=150),
        "review": LLMTier(provider="client", model="client-default", max_tokens=1200, temperature=0.2),
    })
    # Tried in order after the primary of a tier or task tier (keys: tier names and task-tier names).
    fallbacks: dict[str, list[LLMTier]] = Field(default_factory=lambda: {
        "small": [LLMTier(provider="ollama", model="qwen2.5:7b", max_tokens=256)],
        "medium": [LLMTier(provider="ollama", model="qwen2.5:7b", max_tokens=768, num_ctx=4096)],
        "large": [LLMTier(provider="ollama", model="qwen2.5:7b", max_tokens=1500)],
        "narrative": [LLMTier(provider="ollama", model="qwen2.5:7b", max_tokens=1200, temperature=0.6),
                      LLMTier(provider="client", model="client-default", max_tokens=1200)],
        "review": [LLMTier(provider="ollama", model="qwen2.5:7b", max_tokens=1200, temperature=0.2)],
    })
    routing: RoutingCfg = Field(default_factory=RoutingCfg)
    allow_cloud: bool = True        # if False, cloud providers (incl. client) are skipped; falls to local ones
    timeout_s: float = 120.0        # per-call wall clock
    retries: int = 2                # transient-error retries per candidate
    backoff_s: float = 0.5          # base for exponential backoff
    availability_ttl_s: float = 30.0
    retry_on_timeout: bool = False  # a timeout moves to the next faster/other candidate instead of retrying
    client_on_failure: bool = True  # if every local option fails/times out, hand the task to the calling agent
    warmup: bool = True             # preload models in the background on first use (and via router.warmup())
    warmup_tiers: list[str] = Field(default_factory=lambda: ["small"])  # tiers/task tiers to preload
    # Stable per-class system prefixes: identical bytes across calls let Ollama reuse its KV cache.
    system_prefix: dict[str, str] = Field(default_factory=lambda: {
        "field": "You fill job application forms. Answer concisely and factually. No preamble.",
        "batch": "You fill job application forms. Reply with one JSON object mapping each field key to its answer.",
        "narrative": "You write first-person application prose: specific, honest, no clichés.",
        "review": "You review application drafts for specificity, accuracy and tone.",
    })
    heavy_parallel: int = 1         # concurrent calls to `heavy` local models (one big model resident on 16 GB)
    providers: dict[str, dict[str, Any]] = Field(default_factory=lambda: {
        "ollama": {"base_url": "http://127.0.0.1:11434", "keep_alive": "30m"},
        "openai_compat": {"base_url": "", "api_key_env": "OPENAI_API_KEY"},
        "anthropic": {"api_key_env": "ANTHROPIC_API_KEY"},
        "hf_local": {"device": "auto"},
    })


class PolicyCfg(BaseModel):
    max_years_required: int | None = None
    salary_floor: int | None = None              # annual, in the candidate's home market
    col_multipliers: dict[str, float] = Field(default_factory=dict)  # city substring -> multiplier
    # Deterministic salary rule (ADR-0011 Amendment 2). base_salary defaults to salary_floor.
    base_salary: int | None = None
    default_ask: int | None = None               # used when the posting shows no range: ask = max(floor, default_ask)
    salary_round_to: int = 1000                  # clean-number rounding step
    salary_multi_level_ratio: float = 1.4        # top/bottom above this => range spans levels
    salary_lower_middle: float = 0.33            # anchor position inside a multi-level range (0.5 = midpoint)
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
    exclude_title_patterns: list[str] = Field(default_factory=list)  # regexes on the title (e.g. "research")
    has_phd: bool = False                        # False: skip titles aimed at PhD graduates/students (ADR-0011)


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


class ImapCfg(BaseModel):
    """Read-only IMAP (ADR-0016). Holds only *names* of environment variables / a keychain service: the
    username and password themselves are never read from config files."""

    host: str = ""
    port: int = 993
    folder: str = "INBOX"
    user_env: str = "JOB_APPLY_IMAP_USER"
    password_env: str = "JOB_APPLY_IMAP_PASSWORD"
    keyring_service: str = "job-apply-mcp"   # optional `keyring` extra; used when the env vars are unset
    senders: list[str] = Field(default_factory=list)     # extra FROM terms (union with subjects)
    subjects: list[str] = Field(default_factory=lambda: ["application", "applying", "applied"])
    max_messages: int = 500


class IngestCfg(BaseModel):
    """Mail-confirmation dedupe (ADR-0016). `client` needs no credentials in the server."""

    source: str = "client"              # client | mbox | imap
    path: str | None = None             # mbox file or directory of .eml files (source = mbox)
    patterns_file: str | None = None    # user TOML extending the built-in sender/subject patterns
    since_days: int = 90
    min_confidence: float = 0.6
    max_new_shown: int = 10             # compact outputs list at most this many new matches
    max_messages_per_call: int = 300
    imap: ImapCfg = Field(default_factory=ImapCfg)


class PathsCfg(BaseModel):
    data_dir: str = Field(default_factory=paths.default_data_dir_str)
    profile: str = Field(default_factory=lambda: paths.default_config_dir_str() + "/profile.toml")
    resume: str | None = None


class ImproveCfg(BaseModel):
    """Self-improvement (ADR-0015). Everything about repo, remote, branches, coder and path scope is tunable."""

    coder: str = "none"                         # claude_code | aider | client | none
    repo_path: str | None = None                # git checkout used as the base; default: package source repo
    remote: str = "origin"
    base_branch: str = "main"
    branch_prefix: str = "auto/"
    gh_repo: str | None = None                  # "owner/name"; default: gh infers from the remote
    open_pr: bool = True
    worktree_dir: str | None = None             # default: <data_dir>/improve/worktrees
    cleanup_worktree: bool = True               # remove the worktree after a PR is opened
    allowed_paths: list[str] = Field(default_factory=lambda: ["src/", "tests/", "docs/", "examples/"])
    forbidden_paths: list[str] = Field(default_factory=lambda: [
        ".env", ".github/", "uv.lock", "pyproject.toml", "*.pem", "*.key", "secrets", "profile.toml", "config.toml"])
    test_commands: list[list[str]] = Field(default_factory=lambda: [
        ["uv", "run", "--extra", "dev", "ruff", "check", "."], ["uv", "run", "--extra", "dev", "pytest", "-q"]])
    claude_cmd: str = "claude"
    claude_allowed_tools: list[str] = Field(default_factory=lambda: [
        "Read", "Edit", "Write", "Glob", "Grep"])   # file tools only: the harness runs tests/git
    claude_max_turns: int = 40
    aider_cmd: str = "aider"
    aider_model: str = "medium"                 # a router tier name (llm.tiers) or an explicit aider model id
    coder_timeout_s: int = 1800
    context_max_chars: int = 6000
    auto_apply_low_risk: bool = False           # apply config/profile-pattern proposals without a PR
    min_cluster_count: int = 3                  # failures needed before the analyzer proposes anything
    window_days: int = 30


class ProvenanceCfg(BaseModel):
    db_path: str | None = None          # default: <paths.data_dir>/provenance.db
    export_tagging: str = "sidecar"     # none | sidecar | metadata  (see provenance/export.py)


class ApplyCfg(BaseModel):
    auto_approve: bool = False          # True skips the hash-bound approval token (ADR-0011)
    short_text_max: int = 200           # unresolved fields up to this length go to the local LLM
    llm_min_confidence: float = 0.6     # below this an LLM answer is treated as unresolved
    plan_ttl_s: int = 1800              # how long a plan (and its token) stays valid
    context_chars: int = 900            # cap on the posting summary inside client_tasks
    plan_workers: int = 4               # concurrent planners; separate from browser_workers (pipelining)
    write_optional: bool = False        # also write optional long free-text fields (default: skip them)
    option_min_score: float = 0.78      # fuzzy option snap threshold; below it the field stays unresolved
    referral_preference: list[str] = Field(default_factory=lambda: [
        "Job board", "Other job board", "Other", "LinkedIn"])  # "how did you hear"; never Referral/Employee


class BrowserCfg(BaseModel):
    driver: str = "playwright"
    fallback_driver: str | None = "browser_use"   # used for sites the primary driver does not know
    user_data_dir: str = Field(default_factory=lambda: paths.default_data_dir_str() + "/browser-profile")
    headless: bool = False
    keep_pages_open: bool = False       # debugging: leave the job's tab open
    channel: str | None = None          # e.g. "chrome" to use the installed Chrome
    timeout_s: float = 30.0
    upload_settle_s: float = 4.0        # wait after a resume upload (parsers re-render the form)
    confirmation_wait_s: float = 10.0
    evidence: bool = True               # screenshot + text + errors + submit responses under <data_dir>/evidence/<ref-hash>/
    evidence_text_chars: int = 20000    # cap on saved visible page text
    evidence_body_chars: int = 500      # cap on each saved submit-response body
    captcha_handoff: bool = True        # captcha-protected pages (always Ashby): never click submit; human does (ADR-0012). Tests only: False
    presubmit_validation: bool = True   # read back fields + check visible validation errors before the submit click
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
    heavy_model_gb: float = 4.0                   # resident models smaller than this do not block training
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
    improve: ImproveCfg = Field(default_factory=ImproveCfg)
    concurrency: ConcurrencyCfg = Field(default_factory=ConcurrencyCfg)
    llm: LLMCfg = Field(default_factory=LLMCfg)
    policy: PolicyCfg = Field(default_factory=PolicyCfg)
    paths: PathsCfg = Field(default_factory=PathsCfg)
    provenance: ProvenanceCfg = Field(default_factory=ProvenanceCfg)
    apply: ApplyCfg = Field(default_factory=ApplyCfg)
    browser: BrowserCfg = Field(default_factory=BrowserCfg)
    writing: WritingCfg = Field(default_factory=WritingCfg)
    platforms: PlatformsCfg = Field(default_factory=PlatformsCfg)
    control: ControlCfg = Field(default_factory=ControlCfg)
    tune: TuneCfg = Field(default_factory=TuneCfg)
    ingest: IngestCfg = Field(default_factory=IngestCfg)
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
    user = paths.config_file()
    for p in (user, (project_dir or Path.cwd()) / "job-apply-mcp.toml"):
        if p.is_file():
            data = _deep_merge(data, tomllib.loads(p.read_text(encoding="utf-8")))
    data = _deep_merge(data, _env_overrides())
    return Settings.model_validate(data)

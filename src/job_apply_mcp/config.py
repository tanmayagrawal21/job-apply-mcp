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

import logging
import os
import re
import tomllib
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, Field, PrivateAttr, TypeAdapter, ValidationError, field_validator

from . import paths

log = logging.getLogger(__name__)


class HostLimitCfg(BaseModel):
    per_host: int = 4               # max concurrent requests to this host
    min_interval_s: float = 0.25    # minimum spacing between request starts to this host


def _default_host_limits() -> dict[str, HostLimitCfg]:
    # Public job-board APIs built for embedding; modest, documented-friendly rates (ADR-0024).
    return {"boards-api.greenhouse.io": HostLimitCfg(per_host=16, min_interval_s=0.02),
            "api.ashbyhq.com": HostLimitCfg(per_host=8, min_interval_s=0.05),
            "api.lever.co": HostLimitCfg(per_host=8, min_interval_s=0.05)}


class ConcurrencyCfg(BaseModel):
    global_limit: int = 48          # max concurrent outbound tasks (board fan-out); >= the per-host caps summed
    per_host: int = 4               # max concurrent requests per host
    min_interval_s: float = 0.25    # polite spacing per host
    hosts: dict[str, HostLimitCfg] = Field(default_factory=_default_host_limits)   # per-host overrides of the two knobs above
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
        # Required-essay autodraft (ADR-0057): writer and strict reviewer, LOCAL ONLY (llm.local_only_classes).
        # Each is configurable on its own: [llm.task_tiers.essay] model = "..." keeps the other fields.
        # Default from the ADR-0057 benchmark: gemma4:12b was the only local model whose drafts passed; qwen2.5:7b
        # as reviewer returned wrong findings. One model for both = no weight swap between writer and reviewer.
        "essay": LLMTier(provider="ollama", model="gemma4:12b-it-qat", max_tokens=700, temperature=0.4,
                         num_ctx=6144, timeout_s=120),
        "essay_review": LLMTier(provider="ollama", model="gemma4:12b-it-qat", max_tokens=600, temperature=0.0,
                                num_ctx=6144, timeout_s=90),
    })
    # Tried in order after the primary of a tier or task tier (keys: tier names and task-tier names).
    fallbacks: dict[str, list[LLMTier]] = Field(default_factory=lambda: {
        "small": [LLMTier(provider="ollama", model="qwen2.5:7b", max_tokens=256)],
        "medium": [LLMTier(provider="ollama", model="qwen2.5:7b", max_tokens=768, num_ctx=4096)],
        "large": [LLMTier(provider="ollama", model="qwen2.5:7b", max_tokens=1500)],
        "narrative": [LLMTier(provider="ollama", model="qwen2.5:7b", max_tokens=1200, temperature=0.6),
                      LLMTier(provider="client", model="client-default", max_tokens=1200)],
        "review": [LLMTier(provider="ollama", model="qwen2.5:7b", max_tokens=1200, temperature=0.2)],
        "essay": [], "essay_review": [],   # fail closed: no other model approves what the configured one would not
    })
    # Task classes the router runs on LOCAL providers only: cloud and client candidates are skipped and
    # client_on_failure never applies (ADR-0057). Changing it outside the trusted user config disables autodraft.
    local_only_classes: list[str] = Field(default_factory=lambda: ["essay", "essay_review"])
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
        "essay": "You write first-person application prose: specific, honest, no clichés.",
        "essay_review": "You are a strict reviewer of application answers. You fail anything unsupported.",
    })
    heavy_parallel: int = 1         # concurrent calls to `heavy` local models (one big model resident on 16 GB)
    # Task classes whose LOCAL calls always share the heavy semaphore, whatever the tier's `heavy` flag (ADR-0041).
    heavy_classes: list[str] = Field(default_factory=lambda: ["narrative", "review", "essay", "essay_review"])
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
    graduation: str | None = None                # fallback when the profile has no education end date, e.g. "2026-05"
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
    # Set by load_settings only (never from config): True when policy.us_person came from the trusted user config.
    # Only then may the mapper answer a U.S.-person question from it (ADR-0055).
    _us_person_trusted: bool = PrivateAttr(default=False)


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
    # Set by load_settings only (never from config): True when rules_file came from the trusted user config.
    _rules_trusted: bool = PrivateAttr(default=False)


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
    reconcile_delays_min: list[float] = Field(default_factory=lambda: [2, 10, 60])  # confirmation checks after a submit (ADR-0025)
    reconcile_window_hours: int = 72    # a confirmation must arrive within this long after the submit
    reconcile_title_threshold: float = 0.75
    seed_since_days: int = 180          # default look-back for seed_from_confirmations
    imap: ImapCfg = Field(default_factory=ImapCfg)


GRANT_MAX_DURATION_S = 12 * 3600
GRANT_MAX_OTP_WINDOW_MIN = 15


class GrantsCfg(BaseModel):
    """Human-signed, time-limited grants (ADR-0027). Raising either cap is honored only from the user config."""

    max_duration_s: int = GRANT_MAX_DURATION_S    # longest grant `job-apply-mcp grant --for` will issue
    max_uses_cap: int = 1000                      # upper bound for --max-uses
    otp_window_min: int = 10                      # otp_assist reads only a message newer than this
    use_keyring: bool = True                      # store the HMAC secret in the OS keychain when available; only the user config file may disable it
    # consent window (ADR-0048): `job-apply-mcp consent --for 24h|--per-run`; raising either cap is user-config only
    consent_max_duration_s: int = 7 * 86400       # longest window `consent --for` will issue (a week)
    consent_per_run_max_s: int = GRANT_MAX_DURATION_S   # a --per-run consent also expires after this
    otp_unattended_wait_s: int = 180              # otp_unattended: how long to wait for the code mail before `unknown`
    otp_poll_s: int = 15                          # otp_unattended: mail poll interval while waiting


AUTO_ATTEST_CLASSES = ("accuracy", "policy_ack", "background_check", "arbitration_terms")


class ConsentCfg(BaseModel):
    """What an owner-minted consent window (ADR-0048) may do beyond approving submissions. ADR-0065: inside an active
    `approve_submissions` window, required checkboxes of these COMMON attestation classes are ticked automatically
    (deny-by-default classifier in apply/attest.py; every tick is logged). Honoured ONLY from the trusted user config;
    a project file, JOB_APPLY__* or an untrusted user file can only remove classes. Default: none."""
    auto_attest: list[Literal["accuracy", "policy_ack", "background_check", "arbitration_terms"]] = Field(
        default_factory=list)

    @field_validator("auto_attest", mode="before")
    @classmethod
    def _csv(cls, v: Any) -> Any:      # "accuracy,policy_ack" (environment spelling) -> a list
        return [x.strip() for x in v.split(",") if x.strip()] if isinstance(v, str) else v


class AccountsCfg(BaseModel):
    """ATS logins and accounts from the OS-keychain vault (ADR-0046). Every use also needs an open consent window
    (`consent.active(settings, "credentials")`). Secrets are never in config: only `job-apply-mcp vault add` writes
    them, into the keychain."""

    max_login_attempts: int = Field(2, ge=1, le=2)  # per host per run; a lockout or wrong password stops the host
    auto_create: bool = False           # USER CONFIG FILE ONLY: create an ATS account when no vault entry exists
    signup_email: str = ""              # username for new accounts; "" = the email answer of the application form
    password_length: int = Field(24, ge=20, le=64)


class PathsCfg(BaseModel):
    data_dir: str = Field(default_factory=paths.default_data_dir_str)
    profile: str = Field(default_factory=lambda: paths.default_config_dir_str() + "/profile.toml")
    resume: str | None = None
    state_dir: str | None = None        # security-state stores (consent, grants, approvals, audit); trusted user config only


def state_path(s: Settings) -> Path:
    """Where consent, grants, approvals and the control audit/quota log live (ADR-0054)."""
    d = Path(s.paths.state_dir).expanduser() if s.paths.state_dir else paths.state_dir()
    from .migrate import (
        ensure_state,  # merge any pre-existing state from the old locations; raises when it cannot
    )
    ensure_state(s, d)
    return d


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
    @property
    def auto_approve_active(self) -> bool:
        return self.auto_approve and self.approval_mode == "agent"

    approval_mode: Literal["agent", "human"] = "human"  # human: a human channel must approve (ADR-0021)
    approval_ttl_s: int = 1800          # lifetime of a server-held approval record
    daily_limit: int = Field(25, ge=0)  # max live submits per rolling 24h; 0 = NO LIMIT (removes a safety cap; raising it needs the user config file, ADR-0035)
    daily_limit_per_platform: int = Field(10, ge=0)  # max live submits per platform per rolling 24h; 0 = NO LIMIT (user config file only to raise)
    run_limit: int = Field(10, ge=0)    # max live submits per server run; 0 = NO LIMIT (user config file only to raise)
    auto_approve: bool = False          # USER CONFIG FILE ONLY (env/project file ignored); needs approval_mode="agent"
    short_text_max: int = 200           # unresolved fields up to this length go to the local LLM
    llm_min_confidence: float = 0.6     # below this an LLM answer is treated as unresolved
    plan_ttl_s: int = 1800              # how long a plan (and its token) stays valid
    context_chars: int = 900            # cap on the posting summary inside client_tasks
    plan_workers: int = 4               # concurrent planners; separate from browser_workers (pipelining)
    plan_workers_deferred: int = 16     # concurrent planners when prose is deferred (no narrative calls; ADR-0045)
    write_optional: bool = False        # also write optional long free-text fields (default: skip them)
    option_min_score: float = 0.78      # fuzzy option snap threshold; below it the field stays unresolved
    top_languages: int = 3              # "select the languages you are most proficient in": at most this many (ADR-0030)
    referral_preference: list[str] = Field(default_factory=lambda: [
        "Job board", "Other job board", "Other", "LinkedIn"])  # "how did you hear"; never Referral/Employee


class BrowserCfg(BaseModel):
    driver: str = "playwright"
    fallback_driver: str | None = "browser_use"   # used for sites the primary driver does not know
    user_data_dir: str = Field(default_factory=lambda: paths.default_data_dir_str() + "/browser-profile")
    headless: bool = False
    keep_pages_open: bool = False       # debugging: leave the job's tab open
    keep_unsubmitted_open: bool = True  # a tab the tool FILLED but did not submit stays open for the human (ADR-0067)
    linger_after_disconnect_s: float = 3600.0   # stdio: keep the browser up this long after the client leaves while kept tabs are open (0 = off)
    channel: str | None = None          # e.g. "chrome" to use the installed Chrome
    timeout_s: float = 30.0
    upload_settle_s: float = 4.0        # wait after a resume upload (parsers re-render the form)
    hydrate_wait_s: float = 10.0        # server-rendered forms: wait for the JS app to take over before typing (ADR-0039)
    confirmation_wait_s: float = 10.0
    combobox_commit_s: float = Field(default=20.0, gt=0, le=20.0)   # autocomplete commit budget per field (ADR-0056)
    evidence: bool = True               # screenshot + text + errors + submit responses under <data_dir>/evidence/<ref-hash>/
    evidence_text_chars: int = 20000    # cap on saved visible page text
    evidence_body_chars: int = 500      # cap on each saved submit-response body
    captcha_handoff: bool = True        # captcha-protected pages (always Ashby): never click submit; human does (ADR-0012). Tests only: False
    captcha_policy: Literal["handoff", "attempt"] = "handoff"  # attempt: click Submit once if only an invisible widget (no visible challenge); trusted user config only (ADR-0036)
    final_click: Literal["agent", "human"] = "agent"   # human: fill + verify, a person clicks Submit in the open tab (ADR-0058); tighten-only
    human_click_timeout_s: float = 900.0  # final_click=human: how long the confirmation watcher waits for the click
    human_click_poll_s: float = 3.0       # final_click=human: watcher poll interval (never clicks, types or retries)
    presubmit_validation: bool = True   # read back fields + check visible validation errors before the submit click
    agent_max_steps: int = 40
    agent_tier: str = "medium"          # llm tier whose model drives the generic agent
    block_resources: list[str] = Field(default_factory=lambda: ["image", "font", "media"])
    block_hosts: list[str] = Field(default_factory=lambda: [
        "google-analytics.com", "googletagmanager.com", "segment.io", "segment.com", "hotjar.com",
        "doubleclick.net", "facebook.net", "fullstory.com", "mixpanel.com", "sentry.io"])
    agent_dry_run_fill: bool = False    # the generic agent cannot be guaranteed to stop before submit


class VerificationCfg(BaseModel):
    """Human-provided email/SMS verification codes after a submit click (ADR-0026). The tool never reads mail."""
    enabled: bool = True
    assist: bool = True                 # try a grant-gated otp_assist (ADR-0027) first; a human click is still required
    sender_domains: dict[str, str] = Field(default_factory=lambda: {
        "greenhouse": "greenhouse.io", "lever": "lever.co", "ashby": "ashbyhq.com"})   # ATS -> expected mail sender
    code_ttl_s: int = 600               # how long a pending code prompt stays open before it becomes a handoff
    max_attempts: int = 1               # code entries per prompt (one click each); a bad format costs none
    elicit: bool = True                 # ask the user for the code via MCP elicitation when the client supports it
    notify: bool = False                # desktop notification (macOS/Linux) when a code is pending; never contains the code
    min_length: int = 4                 # format bounds when the page's input declares none
    max_length: int = 12
    typing_delay_ms: int = 60           # real keystroke cadence when entering the code
    trusted_authserv_ids: list[str] = Field(default_factory=list)   # otp_unattended: Authentication-Results ids of YOUR mail server(s); empty = unattended codes OFF; trusted user config only (ADR-0048)


class WritingCfg(BaseModel):
    enabled: bool = True
    style_exemplar: str | None = None   # path outside the repo; few-shot by structure and tone only
    style_corpus: list[str] = Field(default_factory=list)   # more of the user's own letters (files/dirs) = quality baseline
    cover_letter_policy: str = "required_only"   # required_only | always | never (ADR-0023)
    required_closing: list[str] = Field(default_factory=list)   # regexes; the letter must END with one (long form)
    degenerate_max_nonalpha: float = 0.25    # > this share of non-alphabetic tokens = degenerate output
    degenerate_repeat: int = 3               # a 4-gram repeated this many times = loop
    quality_gate: bool = True                # compare drafts with the user's own letters (writing/quality.py)
    quality_tolerance: float = 1.0           # deterministic metric may trail the corpus mean by this many sd
    quality_min_scores: dict[str, int] = Field(default_factory=lambda: {
        "argument": 4, "voice": 4, "precision": 4, "specificity": 4})
    quality_min_problem_overlap: float = 0.08   # share of the posting's stated problem words the draft must engage
    paragraphs_min: int = 4
    paragraphs_max: int = 6
    max_rounds: int = 3
    reviewer: str = "client"            # "client" = review is a client_task; "router" = task_class="review"
    # Hard wall-clock budget (seconds) for ALL prose fields of one plan: writer and reviewer rounds together.
    # Unfinished fields come back as client_tasks marked `timed_out` with any partial draft (ADR-0041). <=0 = off.
    timeout_s: float = 90.0
    # "inline": plan_application drafts prose. "deferred": plan lists prose as client_tasks and the separate
    # draft_answers tool runs writer + reviewer later, so batch planning stays fast (ADR-0041).
    mode: Literal["inline", "deferred"] = "inline"
    local_essays: bool = False          # ADR-0057: local essay autodraft (with reviewer="router"); trusted user config only
    client_essay_words: int = 200       # word limit given to the client for an essay with no max length (ADR-0057)
    review_client_prose: bool = True    # agent mode: client-supplied prose gets the writer path's review before approval; false only from the trusted user config (ADR-0044)
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
        "track record", "extensive experience", "high-stakes", "proven ability", "i possess",
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
    memo_boards: int = 4096                 # boards whose finished postings stay in memory between searches (ADR-0043)
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


class SessionCfg(BaseModel):
    """Search sessions (ADR-0024): the first search_jobs call builds the full result set once; pages read from it."""
    enabled: bool = True
    ttl_s: int = 1800               # a session (and its cursor) expires this long after its last use
    max_sessions: int = 16          # in-memory LRU size
    persist: bool = False           # also keep sessions on disk (survives server restarts)
    max_rows: int = 5000            # rows kept per session


class SearchCfg(BaseModel):
    """Relevance ranking for search_jobs (ADR-0022). Everything is deterministic unless `embeddings` is on."""
    match: str = "any"                   # "title": every query term must be in the title; "any": rank title+description
    rank: bool = True                    # False = legacy newest-first order, no score/why
    skills: list[str] = Field(default_factory=list)   # extra skills on top of the profile's
    seniority: str | None = None         # target level: "entry" | "mid" | "senior" | None (no preference)
    location_order: list[str] = Field(default_factory=list)  # preferred location substrings, best first
    rank_candidates: int = 40            # description-fetch + full scoring for the top N by the cheap pre-score
    description_concurrency: int = 6     # bounded parallel description fetches (also capped by the host limiter)
    board_timeout_s: float = 60.0        # one board's fetch+parse budget; a slower board is reported in errors (ADR-0043)
    min_score: float = 0.0               # drop rows scoring below this (0-100); 0 keeps everything with a query hit
    weights: dict[str, float] = Field(default_factory=lambda: {
        "query": 4.0, "skills": 2.0, "seniority": 1.0, "location": 1.0, "comp": 1.0, "recency": 1.0})
    recency_half_life_days: float = 14.0
    max_why: int = 3
    sessions: SessionCfg = Field(default_factory=SessionCfg)
    embeddings: bool = False             # optional semantic rerank through a local Ollama embedding model
    embed_model: str = "nomic-embed-text"
    embed_url: str = "http://127.0.0.1:11434"
    embed_weight: float = 3.0


class DedupeCfg(BaseModel):
    """Duplicate identity (ADR-0010 amendment). id_first: requisition id / canonical apply URL, with
    company+title only when neither is known. title: legacy company+fuzzy-title matching."""
    mode: str = "id_first"
    # {"brand": "canonical"}: names that are the same employer (parent/brand). Normalized like company names.
    company_aliases: dict[str, str] = Field(default_factory=dict)
    title_threshold: float = Field(0.6, ge=0.0, le=1.0)    # token-Jaccard for the company+title fallback


class IntakeMailCfg(BaseModel):
    """Job links the user emailed (ADR-0045). Read-only: the mailbox is never modified."""
    enabled: bool = True
    source: str | None = None           # mbox | imap | client; None -> ingest.source
    path: str | None = None             # mbox file or .eml directory; None -> ingest.path
    sender: str = "self"                # "self" or one of your own full addresses (self_address / self_aliases)
    recipient: str = "self"             # "self" or one of your own full addresses (self_address / self_aliases)
    self_address: str = ""              # "" -> the profile's contact email, else $<ingest.imap.user_env>
    self_aliases: list[str] = Field(default_factory=list)   # your other full addresses that count as "self"
    query: str = ""                     # case-insensitive text the subject or body must contain (IMAP: TEXT)
    label: str = ""                     # IMAP folder / Gmail label to EXAMINE; mbox: X-Gmail-Labels header (config only)
    since_days: int = 14
    since: str | None = None            # YYYY-MM-DD; wins over since_days
    max_messages: int = 200
    max_links_per_message: int = 20
    peek_bytes: int = 65536             # IMAP BODY.PEEK partial fetch size
    job_url_patterns: list[str] = Field(default_factory=lambda: [
        r"/jobs?(/|$|\?)", r"/careers?(/|$|\?)", r"/positions?/", r"/openings?/", r"/vacanc", r"/requisitions?/",
        r"/job-?postings?/", r"/apply(/|$|\?)", r"[?&](gh_jid|jobid|job_id|jid|reqid|req_id)="])
    ignore_url_patterns: list[str] = Field(default_factory=lambda: [
        r"unsubscribe", r"/preferences", r"\.(png|jpe?g|gif|svg|css|js)(\?|$)", r"/privacy", r"/terms"])


class IntakeDiscoveryCfg(BaseModel):
    query: str = ""
    boards: list[str] = Field(default_factory=list)
    platforms: list[str] = Field(default_factory=list)
    location: str = ""
    since_days: int = 7


class IntakeCfg(BaseModel):
    """run_applications (ADR-0045): explicit links, emailed links and discovery through one pipeline."""
    sources: list[str] = Field(default_factory=lambda: ["links", "mail", "discovery"])
    max_applications: int = Field(10, ge=1)     # default per run; the caller or run_size feedback may change it
    max_applications_cap: int = Field(50, ge=1)  # upper bound for any per-run value
    plan_factor: float = Field(3.0, ge=1.0)     # candidates planned per run = ceil(max_applications * plan_factor)
    submit_parallel: int = Field(4, ge=1)       # global concurrent submits; always at most 1 per host
    writing_mode: Literal["inline", "deferred"] = "deferred"   # batch runs never block on prose
    report_rows: int = 20                       # rows per category in compact reports
    salary_max_multiple: float = Field(3.0, ge=0)   # feedback never raises an ask above this x the largest configured salary value (0 = off)
    mail: IntakeMailCfg = Field(default_factory=IntakeMailCfg)
    discovery: IntakeDiscoveryCfg = Field(default_factory=IntakeDiscoveryCfg)


class PersonaCfg(BaseModel):
    """Profile builder (ADR-0047): gap collection, stored answers and interactive questions."""
    enabled: bool = True              # record gaps and apply stored answers/persona policies
    ask_on_start: bool = True         # ask the top gaps once, at the start of a session, if the client can elicit
    ask_limit: int = 5                # questions per interactive round
    min_hits: int = 1                 # a gap must have been seen on this many applications to be asked


AUTONOMY_LEVELS = ("strict", "safe_default", "infer")


class AnswersCfg(BaseModel):
    """How much the agent may assume when the owner is not available (ADR-0052). Only the trusted user config may
    make it more permissive; a project file or JOB_APPLY__* can only make it stricter."""
    # strict: unresolved fields go to the owner (the behaviour before ADR-0052).
    # safe_default: a deterministic SAFE answer where one exists (decline contact/marketing permission, the
    #   configured "how did you hear" default, decline to self-identify); everything else stays unresolved.
    # infer: safe_default plus ordinary capability/experience choice questions answered conservatively by the LLM.
    autonomy: Literal["strict", "safe_default", "infer"] = "strict"
    promote_after: int = Field(default=3, ge=1)     # assumptions of one question kind before the owner is asked
    infer_min_confidence: float = Field(default=0.8, ge=0.0, le=1.0)   # an affirmative inferred answer needs this
    referral_default: str = ""     # "how did you hear" default; "" = the first apply.referral_preference entry


class Settings(BaseModel):
    # ADR-0048: False when the mail settings otp_unattended relies on (sender domains, mail source, IMAP server,
    # trusted authserv ids) came from anything but the trusted user config. Fail closed: False until load_settings
    # has verified them; programmatic Settings must opt in explicitly.
    _otp_mail_trusted: bool = PrivateAttr(default=False)
    # ADR-0057: True only when the trusted user config itself sets writing.reviewer="router" and no project file,
    # environment variable or untrusted file changed the writer/reviewer models, rules or provider settings.
    # Fail closed: False until load_settings has verified it; programmatic Settings must opt in explicitly.
    _autodraft_trusted: bool = PrivateAttr(default=False)
    # ADR-0065: True only when the trusted user config itself sets [consent].auto_attest. Fail closed: False until
    # load_settings has verified it; programmatic Settings must opt in explicitly.
    _auto_attest_trusted: bool = PrivateAttr(default=False)
    consent: ConsentCfg = Field(default_factory=ConsentCfg)
    persona: PersonaCfg = Field(default_factory=PersonaCfg)
    answers: AnswersCfg = Field(default_factory=AnswersCfg)
    search: SearchCfg = Field(default_factory=SearchCfg)
    dedupe: DedupeCfg = Field(default_factory=DedupeCfg)
    improve: ImproveCfg = Field(default_factory=ImproveCfg)
    concurrency: ConcurrencyCfg = Field(default_factory=ConcurrencyCfg)
    llm: LLMCfg = Field(default_factory=LLMCfg)
    policy: PolicyCfg = Field(default_factory=PolicyCfg)
    paths: PathsCfg = Field(default_factory=PathsCfg)
    provenance: ProvenanceCfg = Field(default_factory=ProvenanceCfg)
    apply: ApplyCfg = Field(default_factory=ApplyCfg)
    browser: BrowserCfg = Field(default_factory=BrowserCfg)
    verification: VerificationCfg = Field(default_factory=VerificationCfg)
    writing: WritingCfg = Field(default_factory=WritingCfg)
    platforms: PlatformsCfg = Field(default_factory=PlatformsCfg)
    control: ControlCfg = Field(default_factory=ControlCfg)
    tune: TuneCfg = Field(default_factory=TuneCfg)
    ingest: IngestCfg = Field(default_factory=IngestCfg)
    grants: GrantsCfg = Field(default_factory=GrantsCfg)
    intake: IntakeCfg = Field(default_factory=IntakeCfg)
    accounts: AccountsCfg = Field(default_factory=AccountsCfg)
    platforms_enabled: list[str] = Field(default_factory=lambda: ["greenhouse", "lever", "ashby",
                                                                 "smartrecruiters", "workday", "generic"])
    dry_run: bool = True            # safe default: nothing is submitted until the user flips this
    response_budget_chars: int = 4000   # tools truncate/paginate to stay under this

    @property
    def essay_autodraft(self) -> bool:
        """ADR-0057: required essays are drafted locally, strictly reviewed locally and filled (labelled assumed)
        only when the TRUSTED user config chose reviewer="router" and autonomy is safe_default or infer."""
        return (self._autodraft_trusted and self.writing.enabled and self.writing.local_essays
                and self.writing.reviewer == "router"
                and self.answers.autonomy in ("safe_default", "infer"))


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


# Security-relevant settings (ADR-0021, ADR-0027, ADR-0035, ADR-0037). A project file, the environment or an
# untrusted user file may only make these STRICTER than the baseline: the user-config value when the user file sets
# it, else the built-in default. Candidate values are coerced with pydantic's own lax rules first (so "false",
# "0", "500" are judged as the bool/int the model will hold), then compared as typed values. Predicates take
# (candidate, baseline) and return True when the candidate RELAXES the baseline.
def _bool_on(new: Any, base: Any) -> bool:       # True is the risky value
    return bool(new) and not base


def _bool_off(new: Any, base: Any) -> bool:      # False is the risky value
    return not new and bool(base)


def _raises(new: Any, base: Any) -> bool:
    return new > base


def _loosens(new: Any, base: Any) -> bool:
    """Submission caps: 0 = no limit. Loosened by 0 or by a higher value; anything is tighter than 'no limit'."""
    return bool(base) and (new == 0 or new > base)


_RELAXES: dict[tuple[str | None, str], Any] = {
    (None, "dry_run"): _bool_off,
    ("apply", "auto_approve"): _bool_on,
    ("apply", "approval_mode"): lambda new, base: new == "agent" and base != "agent",
    ("control", "apply_enabled"): _bool_on,
    ("control", "dev_mode"): _bool_off,
    ("grants", "max_duration_s"): _raises,
    ("grants", "max_uses_cap"): _raises,
    ("grants", "otp_window_min"): _raises,
    ("grants", "use_keyring"): _bool_off,
    ("grants", "consent_max_duration_s"): _raises,          # ADR-0048
    ("grants", "consent_per_run_max_s"): _raises,
    ("browser", "captcha_handoff"): _bool_off,
    ("browser", "captcha_policy"): lambda new, base: new == "attempt" and base != "attempt",   # ADR-0036
    # ADR-0058: "human" only tightens (any layer may set it); going back to "agent" from a trusted "human" relaxes
    ("browser", "final_click"): lambda new, base: new == "agent" and base != "agent",
    ("writing", "review_client_prose"): _bool_off,                                             # ADR-0044
    ("accounts", "auto_create"): _bool_on,                                                        # ADR-0046
    ("accounts", "max_login_attempts"): _raises,
    # ADR-0052: autonomy may only get stricter outside the trusted user config
    ("answers", "autonomy"): lambda new, base: AUTONOMY_LEVELS.index(new) > AUTONOMY_LEVELS.index(base),
    ("answers", "promote_after"): _raises,
    ("answers", "infer_min_confidence"): lambda new, base: new < base,
    ("answers", "referral_default"): lambda new, base: new != base,
    # ADR-0065: adding any auto-attest class relaxes; removing classes only tightens
    ("consent", "auto_attest"): lambda new, base: bool(set(new) - set(base)),
}
# Whose mail feeds automatic applications (ADR-0045): identity keys, so ANY change from the trusted baseline counts as
# relaxing. Only the trusted user config may set them; a project file or JOB_APPLY__* cannot point "self" elsewhere.
for _k in ("sender", "recipient", "self_address", "self_aliases"):
    _RELAXES[("intake.mail", _k)] = lambda new, base: new != base
_RELAXES[("ingest.imap", "user_env")] = lambda new, base: new != base   # fallback source of the "self" address
# ADR-0055: candidate legal-status facts (gates, and the mapper's U.S.-person fallback). Only the trusted user config
# may set them; a project file, JOB_APPLY__* or an untrusted user file cannot change them in either direction.
for _k in ("us_person", "us_citizen", "requires_sponsorship", "has_clearance"):
    _RELAXES[("policy", _k)] = lambda new, base: new != base

# Submission caps (ADR-0035). `apply.*` is the documented setting; `control.params.quota.*` is the legacy
# spelling and takes precedence when set, so it is guarded too.
CAP_DEFAULTS: dict[str, int] = {"daily_limit": 25, "daily_limit_per_platform": 10, "run_limit": 10}
LEGACY_QUOTA_KEYS: dict[str, str] = {"per_day": "daily_limit", "per_platform_per_day": "daily_limit_per_platform",
                                     "per_run": "run_limit"}
for _k in CAP_DEFAULTS:
    _RELAXES[("apply", _k)] = _loosens

_INVALID = object()
_DIGITS = re.compile(r"[0-9]+")


def parse_cap(v: Any) -> int | None:
    """Strict submission-cap parse (ADR-0037): a non-negative whole number given as an int or a string of ASCII
    digits. Floats (0.5, 1e9, nan), negatives, bools and anything else -> None (invalid; callers fail closed)."""
    if isinstance(v, bool):
        return None
    if isinstance(v, int):
        return v if v >= 0 else None
    if isinstance(v, str) and _DIGITS.fullmatch(v.strip()):
        return int(v.strip())
    return None


def _stricter_cap(a: int, b: int) -> int:
    """The tighter of two caps where 0 = no limit."""
    if a == 0:
        return b
    if b == 0:
        return a
    return min(a, b)


def _adapter(sect: str | None, key: str) -> TypeAdapter:
    model: type[BaseModel] = Settings
    for part in (sect.split(".") if sect else []):   # dotted sections: "intake.mail"
        model = model.model_fields[part].annotation  # type: ignore[assignment]
    return TypeAdapter(model.model_fields[key].annotation)


def _typed(adapter: TypeAdapter, v: Any) -> Any:
    """Coerce like pydantic does when it validates the model; _INVALID if it would be rejected (validation
    then reports it, so an invalid value never silently becomes a relaxed one)."""
    try:
        return adapter.validate_python(v)
    except ValidationError:
        return _INVALID


def _obj(s: Any, sect: str | None) -> Any:
    for part in (sect.split(".") if sect else []):
        s = getattr(s, part)
    return s


def _section(d: dict, sect: str | None) -> Any:
    """The raw dict for a (dotted) section, or None."""
    for part in (sect.split(".") if sect else []):
        d = d.get(part) if isinstance(d, dict) else None
    return d


def _drop(d: dict, sect: str | None, key: str) -> dict:
    """Copy of `d` without sect.key (dotted sections rebuilt along the path)."""
    if not sect:
        return {k: v for k, v in d.items() if k != key}
    head, _, rest = sect.partition(".")
    sub = d.get(head)
    if not isinstance(sub, dict):
        return d
    return {**d, head: _drop(sub, rest or None, key)}


def _get(s: Settings, sect: str | None, key: str) -> Any:
    return getattr(_obj(s, sect), key)


def _quota_params(d: dict) -> dict:
    p = d.get("control", {}).get("params", {}) if isinstance(d.get("control"), dict) else {}
    q = p.get("quota") if isinstance(p, dict) else None
    return q if isinstance(q, dict) else {}


def _legacy_base(base: Settings, key: str) -> int:
    """Effective baseline for a legacy quota key: the baseline's legacy value when set, else its apply.* cap."""
    q = base.control.params.get("quota", {})
    if key in q:
        v = parse_cap(q[key])
        if v is not None:
            return v
    return getattr(base.apply, LEGACY_QUOTA_KEYS[key])


def _relaxations(d: dict, base: Settings) -> tuple[list[tuple[str | None, str, Any]], list[tuple[str, Any]]]:
    """Guarded keys in raw layer `d` whose typed value relaxes `base`: ([(sect, key, raw)], [(quota_key, raw)])."""
    hits = []
    for (sect, key), relaxes in _RELAXES.items():
        tgt = _section(d, sect)
        if isinstance(tgt, dict) and key in tgt:
            v = _typed(_adapter(sect, key), tgt[key])
            if v is not _INVALID and relaxes(v, _get(base, sect, key)):
                hits.append((sect, key, tgt[key]))
    q = _quota_params(d)
    qhits = []
    for k in LEGACY_QUOTA_KEYS:
        if k in q:
            v = parse_cap(q[k])          # invalid counts as loosening: it never reaches effective_limits
            if v is None or _loosens(v, _legacy_base(base, k)):
                qhits.append((k, q[k]))
    return hits, qhits


def _sanitize_caps(d: dict, source: str, base: Settings | None) -> dict:
    """Every submission cap (apply.* and legacy control.params.quota.*) must be a non-negative whole number. An
    invalid value from ANY source (0.5, "0.5", -1, "1e9", nan, ...) is replaced by the stricter of the user
    value (`base`, if known) and the built-in default, and logged. Valid values are normalized to int."""
    out = dict(d)
    ap = out.get("apply")
    if isinstance(ap, dict):
        ap = dict(ap)
        for k, dflt in CAP_DEFAULTS.items():
            if k in ap:
                n = parse_cap(ap[k])
                if n is None:
                    n = _stricter_cap(getattr(base.apply, k), dflt) if base else dflt
                    log.warning("apply.%s=%r from %s is not a whole number >= 0; using %d", k, ap[k], source, n)
                ap[k] = n
        out["apply"] = ap
    q = _quota_params(out)
    if q:
        q = dict(q)
        for k, cap in LEGACY_QUOTA_KEYS.items():
            if k in q:
                n = parse_cap(q[k])
                if n is None:
                    n = _stricter_cap(_legacy_base(base, k), CAP_DEFAULTS[cap]) if base else CAP_DEFAULTS[cap]
                    log.warning("control.params.quota.%s=%r from %s is not a whole number >= 0; using %d",
                                k, q[k], source, n)
                q[k] = n
        ctl = dict(out["control"])
        ctl["params"] = {**ctl["params"], "quota": q}
        out["control"] = ctl
    return out


def _restrict(d: dict, source: str, base: Settings | None = None) -> dict:
    """Drop values that would relax a security setting relative to `base` (default: built-in defaults); log each.
    Stricter values pass."""
    base = base or Settings()
    hits, qhits = _relaxations(d, base)
    out = dict(d)
    for k, raw in qhits:
        log.warning("control.params.quota.%s=%r from %s IGNORED: a submission cap can only be loosened in the "
                    "user config file", k, raw, source)
    if qhits:
        ctl = dict(out["control"])
        params = dict(ctl["params"])
        params["quota"] = {k: v for k, v in _quota_params(out).items() if k not in dict(qhits)}
        ctl["params"] = params
        out["control"] = ctl
    for sect, key, raw in hits:
        log.warning("%s%s=%r from %s IGNORED: security settings can only be relaxed in the user config file",
                    f"{sect}." if sect else "", key, raw, source)
        out = _drop(out, sect, key)
    return out


def _enforce(s: Settings, base: Settings) -> None:
    """Defense in depth on the VALIDATED model: any guarded value still looser than the baseline is reset."""
    for (sect, key), relaxes in _RELAXES.items():
        if relaxes(_get(s, sect, key), _get(base, sect, key)):
            log.error("%s%s=%r relaxed past the user config; reset to %r", f"{sect}." if sect else "", key,
                      _get(s, sect, key), _get(base, sect, key))
            setattr(_obj(s, sect), key, _get(base, sect, key))
    q = s.control.params.get("quota")
    if isinstance(q, dict):
        for k in LEGACY_QUOTA_KEYS:
            if k in q:
                v = parse_cap(q[k])
                if v is None or _loosens(v, _legacy_base(base, k)):
                    log.error("control.params.quota.%s=%r relaxed past the user config; reset", k, q[k])
                    q[k] = _legacy_base(base, k)


def _mail_view(s: Settings) -> tuple:
    """Everything otp_unattended trusts to pick the code mail (ADR-0048)."""
    return (dict(s.verification.sender_domains), list(s.verification.trusted_authserv_ids), s.ingest.source,
            s.ingest.path, s.ingest.imap.model_dump())


def _autodraft_view(s: Settings) -> tuple:
    """Everything the essay autodraft trusts (ADR-0057): reviewer choice, prose rules, the essay writer/reviewer tiers
    and their fallbacks, the local-only list, cloud permission and provider endpoints."""
    w, llm = s.writing, s.llm
    tiers = {k: (llm.task_tiers[k].model_dump() if k in llm.task_tiers else None,
                 [t.model_dump() for t in llm.fallbacks.get(k, [])]) for k in ("essay", "essay_review")}
    return (w.reviewer, w.local_essays, w.enabled, list(w.rules), list(w.banned_phrases), list(w.gap_patterns), w.max_rounds,
            w.style_exemplar, list(w.style_corpus), w.timeout_s, tiers, list(llm.local_only_classes), llm.allow_cloud,
            repr(sorted(llm.providers.items())), dict(llm.system_prefix))


def _seed_task_tiers(d: dict, defaults: Settings) -> dict:
    """Per-tier configuration (ADR-0057): `[llm.task_tiers.<class>]` and `[llm.fallbacks.<class>]` change ONE task tier
    (fields missing from a task tier keep their default) instead of replacing the whole table."""
    llm = d.get("llm")
    if not isinstance(llm, dict) or not any(k in llm for k in ("task_tiers", "fallbacks")):
        return d
    seed = {"llm": {"task_tiers": {k: v.model_dump() for k, v in defaults.llm.task_tiers.items()},
                    "fallbacks": {k: [t.model_dump() for t in v] for k, v in defaults.llm.fallbacks.items()}}}
    return _deep_merge(seed, d) if isinstance(llm.get("task_tiers", {}), dict) else d


def _user_file_trusted(user: Path) -> tuple[bool, str]:
    """Only the FIXED-location user config (never one found through XDG_CONFIG_HOME or the environment), owned by this user and not group/world-writable, may relax
    security settings (ADR-0021). A file chosen via JOB_APPLY_MCP_CONFIG is never trusted for that."""
    default = paths.trusted_config_file()
    try:
        if user.resolve() != default.resolve():
            return False, "not the platform-default user config path"
        if os.name != "nt":
            st = user.stat()
            if st.st_uid != os.getuid():
                return False, "not owned by the current user"
            if st.st_mode & 0o022:
                return False, "group/world-writable"
    except OSError as e:
        return False, str(e)
    return True, ""


def load_settings(project_dir: Path | None = None) -> Settings:
    data: dict = {}
    udata: dict = {}
    trusted_user = False
    defaults = Settings()
    user = paths.config_file()
    if user.is_file():
        udata = _seed_task_tiers(_sanitize_caps(tomllib.loads(user.read_text(encoding="utf-8")), str(user), None),
                                 defaults)
        trusted, why = _user_file_trusted(user)
        trusted_user = trusted
        if not trusted:
            udata = _restrict(udata, f"{user} ({why}; can only make settings stricter)", defaults)
        data = _deep_merge(data, udata)
    # Baseline for every later layer: the user's own (typed) values where set, else the defaults. A project file or
    # the environment can tighten it but never loosen it, so a user's cap of 5 cannot be raised back to 25.
    base = Settings.model_validate(udata) if udata else defaults
    proj = (project_dir or Path.cwd()) / "job-apply-mcp.toml"
    if proj.is_file() and proj.resolve() != user.resolve():
        pdata = _seed_task_tiers(_sanitize_caps(tomllib.loads(proj.read_text(encoding="utf-8")), str(proj), base),
                                 base)
        data = _deep_merge(data, _restrict(pdata, str(proj), base))
    edata = _seed_task_tiers(_sanitize_caps(_env_overrides(), "environment (JOB_APPLY__*)", base), base)
    data = _deep_merge(data, _restrict(edata, "environment (JOB_APPLY__*)", base))
    s = Settings.model_validate(data)
    _enforce(s, base)
    ustate = (udata.get("paths") or {}).get("state_dir") if isinstance(udata.get("paths"), dict) else None
    s.paths.state_dir = ustate if trusted_user and isinstance(ustate, str) and ustate else None   # ADR-0054
    s._otp_mail_trusted = _mail_view(s) == _mail_view(base) and (trusted_user or _mail_view(base) == _mail_view(defaults))
    if not s._otp_mail_trusted:
        log.info("otp_unattended disabled: mail settings come from a project file, the environment or an untrusted "
                 "user config (ADR-0048)")
    if trusted_user:     # log which settings the trusted user file relaxed relative to the built-in defaults
        hits, qhits = _relaxations(udata, defaults)
        for sect, key, raw in hits:
            log.warning("security setting %s%s=%r is relaxed by the trusted user config %s",
                        f"{sect}." if sect else "", key, raw, user)
        for k, raw in qhits:
            log.warning("submission cap control.params.quota.%s=%r is loosened by the trusted user config %s",
                        k, raw, user)
    s._autodraft_trusted = (trusted_user and base.writing.reviewer == "router" and base.writing.local_essays
                            and _autodraft_view(s) == _autodraft_view(base))
    if base.writing.local_essays and not s._autodraft_trusted:
        log.info("essay autodraft disabled: writing.reviewer='router' is not from the trusted user config, or a project "
                 "file / the environment changed the prose models or rules (ADR-0057)")
    ucon = udata.get("consent") if isinstance(udata.get("consent"), dict) else {}
    s._auto_attest_trusted = (trusted_user and "auto_attest" in ucon
                              and set(s.consent.auto_attest) <= set(base.consent.auto_attest))      # ADR-0065
    if s.consent.auto_attest and not s._auto_attest_trusted:
        log.info("consent.auto_attest disabled: it is not set by the trusted user config (ADR-0065)")
    upol = udata.get("policy") if isinstance(udata.get("policy"), dict) else {}
    s.policy._us_person_trusted = (trusted_user and "us_person" in upol
                                   and s.policy.us_person == base.policy.us_person)          # ADR-0055
    urf = udata.get("control", {}).get("rules_file") if isinstance(udata.get("control"), dict) else None
    if urf and trusted_user and urf == s.control.rules_file:
        s.control._rules_trusted = True
    if s.apply.auto_approve:
        if s.apply.approval_mode == "human":
            log.warning("apply.auto_approve=true is IGNORED because apply.approval_mode='human'")
        else:
            log.warning("!!! apply.auto_approve=true: real submissions need NO approval id (agent mode). "
                        "Set it back to false unless you accept unattended submissions !!!")
    return s

# Changelog

Format: [Keep a Changelog](https://keepachangelog.com/en/1.1.0/); versions follow [SemVer](https://semver.org/).

## [Unreleased]

- `browser.keep_unsubmitted_open` (default true): tabs the tool filled but did not submit stay open and are listed
  in `<data_dir>/kept_tabs.json`; on stdio the process lingers after a client disconnect while such tabs exist
  (`browser.linger_after_disconnect_s`). Handoff reasons are categorised `hard_limit` / `unknown_fact` / `tool_gap`
  (`cat` on submit rows, `handoff_categories` in run reports); consented steps are tool gaps (ADR-0067).
- Consent-window attestations `[consent] auto_attest` (trusted user config only; default none): inside an active
  `approve_submissions` consent window, REQUIRED checkboxes of the opted-in classes (accuracy, policy_ack,
  background_check, arbitration_terms) are ticked by a deny-by-default classifier; AI-use, export control,
  citizenship, criminal history, restrictive covenants, specific facts and opt-ins are never ticked. Re-checked at
  submit; every tick is logged (class, label, ref, consent id) in evidence, the audit log and the run report (ADR-0065).
- Browser driver (ADR-0063): a Greenhouse board that redirects to a company site is reopened as the Greenhouse
  embed (#98, #99); field lookup is scoped to the application form when a page has several forms (job-alert boxes,
  #98); DOM self-ID surveys use the profile with closed equivalences (Male=Man, explanatory parentheticals, Yes/No
  leads) and are rescanned for conditional questions (#97, #100); the demographic-data processing consent box is
  ticked only with the privacy opt-in, a committed profile demographic answer and an exact template (#97); Lever
  pronouns groups are filled only from `[demographics].pronouns`.
- Any-client usability: a missing optional component (local model, reviewer, browser extra) now returns
  `status: needs_human` with `why` and `next_action` instead of an error (ADR-0059); every status carries a
  machine-readable `next_action` and a one-line `hint` (ADR-0059); `submit_application(final_click="human")`
  fills and verifies only, tighten-only, and skips the API path (ADR-0060); full question labels (up to 500 chars)
  and options reach the client; `health`/`doctor` check that the resume is readable by the server process and
  accept a copy in `<data_dir>/documents`; server `instructions` give the canonical flow and rules (ADR-0061).
- Autonomy level `[answers] autonomy = strict | safe_default | infer` (trusted user config only; default strict):
  safe declines for genuine contact/marketing permission asks, the configured "how did you hear" default and
  "decline to self-identify"; at `infer`, conservative resume-based answers to an allowlist of skill/experience questions. Hard limits
  (authorization, background, attestations, legal agreements, salary, start date/location) are never assumed.
  Assumed answers are labelled in plan, evidence and run report; repeated kinds become one profile question
  (ADR-0052).
- Human-provided email/SMS verification codes: `awaiting_code` state, `provide_verification_code`, MCP elicitation, optional desktop notification; the tool never reads mail and never stores the code (ADR-0026).
- Human-signed, time-limited grants (`job-apply-mcp grant`), `grants_status`, `otp_assist` (ADR-0027).
- Consent window: `job-apply-mcp consent --for 24h | --per-run [--max N] [--scopes ...]` (TTY only), `--status`,
  `--revoke`, read-only `consent_status`; inside it `approval_mode=human` approves automatically (logged with the
  consent id), `captcha_attempt` applies ADR-0036 to hidden widgets, and `otp_unattended` enters an emailed code
  from the newest message of the expected sender (no mail in time -> `unknown`, never retried). Interface for
  other modules: `consent.active(settings, scope)` (ADR-0048).
- Search sessions, per-host rate limits, progress + `search_status` (ADR-0024).

### Fixed
- Mapper: authorization/sponsorship on confirmed home-country jobs recognises more wordings (Greenhouse offices named after a country confirm it, "retain or extend", misspelled "sponsorship", company name in a country-naming question) from structured facts only; current/previous employer and title, prior employment with the posting company (before any inference), country/city/state of residence, "located in X" / "within N miles of X" when computable and relocation/hybrid/onsite preferences are answered from profile facts, otherwise unresolved with a `profile_gaps` entry (ADR-0053, #76, #77).
- `run_applications`: `max_applications` must be a whole number >= 1 and caps eligible postings the same in dry and live mode; one documented status per posting (`dry_run_ok`, `needs_human`, `quota_blocked`, ...; old `handed_off`/`dry_run` rows map on read); dry runs return `live_preview` of the live quotas and never mark a mail message processed; a `quota_blocked` or cap-skipped link keeps its message pending. `plan_application` / `draft_answers` page with `cursor` (from memory, no re-plan); the shaper never emits a negative `next` (ADR-0049, #71).
- Browser driver: Ashby radio/checkbox groups are selected and verified; the required-field scan counts radio/checkbox groups (an unchecked required group is `incomplete`, never `dry_run`); DOM-only consent radios stop the run unless they are an opted-in privacy-notice acknowledgement; server-rendered forms are filled only after hydration (`browser.hydrate_wait_s`); the phone-widget country comes from the profile; DOM-only self-ID selects use only the profile value or the decline option (optional ones without a profile value stay blank); select read-backs are scoped to the field (ADR-0039).

### Added
- `run_applications` tool and `job-apply-mcp run` (explicit links, `--links` file, job links emailed to yourself from the read-only mail source, discovery; any mix, in that priority), `run_report` / `job-apply-mcp report`, `give_feedback` / `job-apply-mcp feedback` applied at the start of the next run, `unsupported_site` rows, concurrent planning, per-host serialized concurrent submits, deferred prose for batch runs, salary feedback bounded by the configured floor, authorization/demographic answers routed to the profile builder (ADR-0045).
- Credential vault and ATS logins (ADR-0046): `job-apply-mcp vault add|list|rm` (TTY only, keychain via the `keyring` extra, no plaintext fallback), read-only `vault_status`, consent-gated (`consent.active(settings, "credentials")`) sign-in on login walls with at most 2 attempts per host per run, optional account creation (`accounts.auto_create`, trusted user config only, default off) with generated passwords stored in the keychain before the form is filled, `account_flags` in `apply_batch`, and `Secret` / log and tool-output redaction.
- `writing.timeout_s` hard budget for all prose of one plan (timed-out fields become `client_tasks` with any partial draft, never answers), per-call provider timeouts, `llm.heavy_classes` semaphore with `llm_wait` / `llm_gen` in `timings`, `writing.mode = "deferred"` plus the `draft_answers` tool (ADR-0041).
- Faster `search_jobs` (issue #60): in-memory memo of parsed boards keyed by response digest, date filter before parsing, guarded text-flag regexes, 404 negative caching, interleaved per-platform fan-out, `search.board_timeout_s`, and a `timings` block in the response. 1,887 boards: cold 333 s -> ~150 s, warm 208 s -> 1.5 s (ADR-0043).
- Optional submission caps `apply.daily_limit` / `daily_limit_per_platform` / `run_limit` (`0` = no limit; loosening only from the trusted user config), `daily_limit` block in `control_status` and `doctor` (ADR-0035).
- Resume-first fact precedence, placeholder/addressee/temporal/degeneration/closing lint, comparative quality gate,
  `facts_report` tool, `writing.cover_letter_policy`, tuning degeneration/leakage hard gates (ADR-0023).
- `job-apply-mcp` CLI with subcommands `serve` (default, stdio), `init`, `doctor`, `version`; `python -m job_apply_mcp`;
  console script `job-apply-mcp-init` (ADR-0020).
- Setup wizard: creates `config.toml`, `profile.toml`, `controls.toml` from the fictional examples, detects
  Ollama / LM Studio / mlx_lm.server, picks model sizes for the machine's RAM, always writes `dry_run = true`,
  never overwrites without `--force`, prints the client config snippet.
- `doctor`: config, profile, models, browser and data-directory checks with fixes (`--json`, `--offline`).
- Streamable HTTP transport (`--transport http`), loopback by default, bearer token from `JOB_APPLY_MCP_TOKEN`
  required for any non-loopback bind, unauthenticated `/healthz`.
- Client setup guides for ten clients in `docs/clients/`; `docs/tools.md`, `docs/configuration.md` (generated),
  `docs/faq.md`; SECURITY, CODE_OF_CONDUCT, issue and PR templates; Dockerfile.
- PyPI-ready metadata (classifiers, URLs, SPDX license, `py.typed`); examples are bundled in the wheel.

### Changed
- Default config/data locations follow the platform (Windows `%APPDATA%` / `%LOCALAPPDATA%`; POSIX unchanged).
- All text files are read and written as UTF-8 (fixes Windows code pages).
- `hf_local` device defaults to `auto` (cuda, then mps, then cpu) instead of `mps`.
- CI runs on Ubuntu, macOS and Windows with Python 3.11 to 3.13.

### Security
- Trust-boundary guards compare typed values: string settings such as `daily_limit = "0"` or `dry_run = "false"` in a project file or env no longer bypass them, and env/project can no longer loosen the user config's own tighter value. `grants.use_keyring` and `browser.captcha_handoff` are now guarded. Submission caps must be whole numbers >= 0; an invalid value (`0.5`, `"1e9"`, `-1`, `nan`) from any source falls back to the stricter of the user value and the default instead of meaning "no limit" (ADR-0037).
- Grant use counts are the max of the signed grant record counter, the audit log and a high-water mark (keychain or a 0600 `grant_uses.json`), so rolling back any one of them no longer restores `max_uses`, and a duplicated grant row is rejected as tampering; grants issued before this change must be re-issued (ADR-0037).

## [0.1.0]
- Initial development release: platform adapters, apply engine, LLM router, control layer, provenance, ingest,
  self-improvement loop, privacy guard.

---
name: applywise
description: How to use the ApplyWise (job-apply) MCP tools safely to search for jobs, run batches of applications from links, emailed links or discovery, dry-run them and hand real submits to the user for approval. Use when the user asks to find jobs, apply to a job or a batch of jobs, check a run or application status, build their profile, or set up ApplyWise.
---

# Using ApplyWise safely

ApplyWise is an MCP server (`plugin:applywise:applywise` in `/mcp`) that searches job boards and fills
application forms. It stops before anything irreversible. Drive the tools, keep the user in control, and never
work around a stop.

## Hard rules

These hold in every mode and configuration. The server enforces them too; do not try to get around them.

- **Never see, ask for or type a password.** Logins live in the OS keychain, added by the user with
  `job-apply-mcp vault add <host> --username U`. The server fills them straight into the page, only while a
  consent window with the `credentials` scope is open; you only get `vault_status` (hosts and counts, never
  secrets). Account creation additionally needs `accounts.auto_create = true` in the user's own config.
- **Never solve, click through or bypass a CAPTCHA or other bot check.** A visible challenge returns
  `needs_human` or `handoff`. Tell the user which page is open and that the final click is theirs.
- **Never read the user's mail to find a verification code.** On `awaiting_code`, ask the user to type the code,
  then call `provide_verification_code`. Unattended codes are read by the server itself, never by you (section 5).
- **Never tick an attestation** the server flagged (for example "I did not use AI"). The user decides.
- **Never invent facts or answer for the user.** Answers come from the profile, the resume and answers the user
  typed. If a required field is unresolved, ask the user; do not guess.
- **Never run the human-only commands**: `job-apply-mcp approve`, `deny`, `resolve`, `grant`, `consent`, `vault`
  and `profile ask|answer`. They need the user's own terminal. Print the exact command and let the user run it.
- **Never edit the user's config to loosen a safety setting** (`dry_run`, `approval_mode`, `auto_approve`,
  `captcha_policy`, `accounts.auto_create`, `verification.trusted_authserv_ids`, limits, grant or consent caps).

## 1. Set up and check (init, doctor)

On first use, call `health`. If it reports missing config or profile, ask the user to run these in their own
terminal (same pin as the plugin's `.mcp.json`):

```bash
uvx --from git+https://github.com/tanmayagrawal21/job-apply-mcp@ecdde2d1d4ceddffedd01605586f592f8e6669d5 job-apply-mcp init
uvx --from git+https://github.com/tanmayagrawal21/job-apply-mcp@ecdde2d1d4ceddffedd01605586f592f8e6669d5 job-apply-mcp doctor
```

`init` writes `config.toml`, `profile.toml` and `controls.toml` and prints their paths. The user fills in the
profile; do not ask them to paste personal details into the chat. Run `/reload-plugins` after changes.

**Work authorization is per country; there is no default country.** Flat `[authorization]` keys describe
`home_country` (else `[contact].country`); other countries need `[authorization.<CC>]` tables. A question about a
country without facts stays unresolved, and authorization or sponsorship questions are answered only from these
structured facts. Point the user at the gap; never assume a country.

## 2. Profile builder

Fields the profile could not answer are collected as gaps. `profile_questions` lists the top ones;
`answer_profile_question(key, scope)` opens a form where the **user** types the answer (it takes no value from
you and stores nothing if the client cannot show a form). In a terminal: `job-apply-mcp profile ask`. Stored
answers fill gaps only; they never override the profile. Demographics come only from the profile.

## 3. Single jobs: search, plan, dry run, review, approve

1. `search_jobs` with a small `limit` and specific `boards` or `query`; page with `next`. Gates drop non-fits.
2. `get_job` only for the few that matter; `"description"` only for finalists.
3. `plan_application` per finalist. Answer only its `client_tasks`, briefly, from the user's real facts. It
   returns an `approval_id` bound to a hash of the exact answers.
4. `submit_application` without changing `dry_run`. Global `dry_run = true` (default) means nothing is sent; a
   per-call `dry_run=false` cannot override it.
5. Show the user the company, title and answers, especially free text. Edits re-bind the approval.
6. A real submit needs `dry_run = false` in the user's own config plus an approval (section 4).

`apply_batch` does steps 3 to 6 for many refs; rows needing input come back `needs_input`, never skipped silently.

## 4. Runs: links, emailed links, discovery

`run_applications` (CLI: `job-apply-mcp run`) handles one batch from any mix of `sources`, in this priority:
explicit `links` (or `links_file`, one per line), job links the user **emailed themselves** (read-only mail
source; sender and recipient default to the user and are config-only; you may narrow with `mail.query`/`since`),
then discovery. Pass `max_applications` (whole number >= 1) when the user names a size; it caps eligible
postings the same in dry and live mode. Daily (25), per-platform (10) and per-run (10) limits still apply on top.
A dry run returns `live_preview` with what live quotas would allow.

Each posting gets exactly one status: `submitted`, `confirmed`, `dry_run_ok`, `needs_human` (`why` says what),
`quota_blocked`, `skipped` (`why`), `unsupported_site`, `unknown` (verify by hand; never retried) or `failed`.
`run_report` reads a run back; `give_feedback` records what to change next run (deny a company or title, run
size, salary, answers; sensitive answers go through `answer_profile_question`).

## 5. Approval, consent window and unattended codes

- **`approval_mode = "human"` (default).** Every real submit needs a person: an MCP confirm dialog, or a
  `needs_human` result with a command such as `job-apply-mcp approve <id>` for the user to run.
- **`approval_mode = "agent"`.** You may submit with the server-issued `approval_id`. Only the user's own config
  can turn this on.
- **Consent window.** The user may run `job-apply-mcp consent --for 24h` (or `--per-run`, `--max N`,
  `--scopes ...`) once in a terminal. Inside it, submissions are approved automatically (logged), a page whose
  only bot check is invisible gets one click, ATS logins may be used, and an emailed code may be entered. The code
  path is the server's: it reads only the newest message from the platform's configured sender, and only when
  `verification.trusted_authserv_ids` lists the user's own mail server; otherwise it stays with the human.
  `consent_status` is read-only; suggest a window only when the user wants unattended runs, and say what it allows.
- **Grants** (`job-apply-mcp grant ...`) let an agent do one human step for a limited time; read with
  `grants_status`. Do not ask for one to save time.

Even with consent or agent mode, visible CAPTCHAs, flagged attestations and unresolved fields still stop.

## 6. Fields the server never answers for the user

- **Contact and messaging permissions** (SMS, WhatsApp, calls, email, marketing, job alerts, talent community)
  are owner-only: never Yes, never an automatic No. Only the user's own `optin:<channel>` answer applies, and only
  to a single, plain permission question; anything else stays unresolved. Do not fill these yourself.
- Attestations, EEO/demographics beyond the profile, legal and export-control questions follow the profile or
  stay unresolved.
- **Coming:** an `answers.autonomy` setting will let the user choose how much the server may answer on its own
  (for example safe default declines). Until it ships, the rules above apply.

## 7. CAPTCHAs and handoff

The default `captcha_policy = "handoff"` fills and checks the form, then stops before the final click when the
page has any CAPTCHA widget, leaving the tab open. The user reviews and clicks submit; `reconcile_submissions`
then settles it from the confirmation email, or the user runs `job-apply-mcp resolve <ref> submitted`.
`captcha_policy = "attempt"` (user config only) or a consent with `captcha_attempt` allows one click when the
only widget is invisible. Nothing ever solves a challenge.

## Keeping replies small

Tools return compact JSON. Use small `limit` values, request `fields` only when needed, use `run_report` with a
`category` instead of whole reports, and pass back short `ref` values instead of URLs or descriptions.
`control_status` shows quota use; when a cap is reached, stop and tell the user rather than suggesting a raise.

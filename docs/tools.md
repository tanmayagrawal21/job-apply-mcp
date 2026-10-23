# Tool reference

Every tool returns **compact JSON** (no whitespace) as a string, truncated to `response_budget_chars`
(default 4000, about 1000 tokens). A truncated reply ends with `"truncated":<original length>`; narrow the
request (fewer fields, smaller `limit`) instead of raising the budget. Lists paginate with `limit` and `cursor`
and return `next` (the cursor for the following page, or null). Large inputs are never echoed back; later
calls take the short `ref` returned earlier.

Tool names are the MCP tool names your client shows. Argument defaults are shown after `=`.


Every tool returns `{"ok":true,...}` or `{"ok":false,"error":{"code","message","hint"}}` (ADR-0005 Amendment 1).

## Discover

| Tool | Inputs | Output | Token notes |
|---|---|---|---|
| `search_status` | `session_id` | `{session,status,boards_total,boards_done,boards_failed,rows,elapsed_s,total}` | Progress of a search session; accepts a session id or cursor. |
| `search_jobs` | `query=""`, `boards=None` (e.g. `["greenhouse:acme"]`), `platforms=None`, `location=""`, `since_days=7`, `limit=25`, `cursor=""`, `apply_policy=True` | `{rows:[{ref,co,title,loc,pay,flags}], total, next, session, errors}` | First call fans out once into a search session (ADR-0024); pass `next` back as `cursor` for instant pages from the session (expired sessions return `error`). Emits MCP progress notifications. ~25 tokens per row. Policy gates drop obvious non-fits (sponsorship, citizenship, years, graduation window) before they reach you. With no boards configured it returns a `hint`. |
| `get_job` | `ref_or_url`, `fields=None` | one posting row; add `"description"` to `fields` for the trimmed text | Description is capped by `platforms.max_description_chars` (6000). Ask for it only for finalists. |
| `get_form` | `ref_or_url` | `{ref, via, fields:[{key,label,kind,required,options}]}` | Options lists are the expensive part; call only before manual answers. |

## Apply

| Tool | Inputs | Output | Token notes |
|---|---|---|---|
| `plan_application` | `ref_or_url`, `cursor=0` | `{ref, gate, why?, answers, src, unresolved, client_tasks?, approval_id?, t, next?, total?}` | Runs gates and duplicate check, fills from profile and local LLMs. `client_tasks` are the only things you must write (short, with bounded context). `src` names every answer source other than `profile`. A result over the response budget is paged: pass `next` as `cursor` until it is null; later pages come from memory, with no re-plan (ADR-0049). |
| `draft_answers` | `ref_or_url`, `cursor=0` | same shape as `plan_application` | Runs the local writer + reviewer for prose fields a plan left open (`writing.mode="deferred"` lists them with `deferred:true`; a `timed_out` task carries a partial `draft`). Bounded by `writing.timeout_s`; re-issues `approval_id` over the grown answer set (ADR-0041). |
| `submit_application` | `ref_or_url`, `answers=None`, `dry_run=None`, `approval_id=None` | `{ok, ref, status, via, confirmation?, detail?}`; status is one of `submitted`, `dry_run`, `duplicate`, `needs_human`, `needs_review` (+`client_tasks`: supplied prose that failed review, agent mode, ADR-0044), `roadblock`, `error`, `awaiting_code` (+`pending_id`) | Real submits need the server-issued `approval_id` from `plan_application` (human approval unless `approval_mode=agent`). Global `dry_run` wins. |
| `provide_verification_code` | `pending_id`, `code` | same row shape as `submit_application` (`submitted`, `awaiting_code` on a bad format, or `handoff`) | The USER types the code they received by email/SMS after a submit returned `awaiting_code`; ask them, never read their mail. Format-checked only, typed into the open page, verify clicked once, no resubmit. Expires after `verification.code_ttl_s` (ADR-0026). |
| `resolve_submission` | `ref`, `outcome=""`, `note=""`, `evidence=""` | `{ok, state, ...}` | Only exposed when `apply.approval_mode=agent`; resolves a submission stuck in submitting/unknown, evidence folder required. |
| `apply_batch` | `refs`, `dry_run=None`, `max_parallel=None` | `{results:[{ref,status,...}]}` one row per ref; unresolved items come back as `needs_input` with `client_tasks`, never silently skipped | Use for dozens of refs; rows stay under ~60 tokens each unless they carry `client_tasks`. |
| `list_applications` | `status=""`, `limit=50`, `cursor=0` | `{rows:[{ref,co,title,st,...}], next, total}` | Local store only. |

`needs_human` is returned for CAPTCHAs, verification codes, account creation, attestation checkboxes that match
`policy.stop_phrases`, and unresolved required fields. See the safety model in the [README](../README.md#safety-model).

## Application runs (ADR-0045)

| Tool | Inputs | Output |
|---|---|---|
| `run_applications` | `links=None`, `links_file=None`, `sources=None` (links, mail, discovery), `max_applications=None`, `dry_run=None`, `mail=None` (narrowing only: {query, since, since_days}; sender, recipient and label are config-only), `messages=None` (client mail source), `discovery=None` (search_jobs arguments) | the run report: `run_id`, `mode`, `max_applications`, `counts`, rows per status with `why` (see [Run statuses](../README.md#run-statuses)), `plan_context` (feedback applied, notes) and, on a dry run, `live_preview` {would_submit, quota_blocked, blocked, warning}. `max_applications` must be a whole number >= 1 and caps eligible postings the same in dry and live mode. Priority: explicit links, emailed links, discovery |
| `run_report` | `run_id=""` (latest), `category=""`, `limit=20` | submitted, confirmed, dry_run_ok, needs_human, quota_blocked, skipped, unsupported_site, unknown and failed rows, each with `why` (old names `handed_off`/`dry_run` accepted as `category`) |
| `give_feedback` | `run_id`, `feedback` (free text or {kind: deny_company, deny_title, answer, profile_answer, salary, run_size, ...}), `ref=None` | the stored feedback and what the next run will apply. Salary feedback never goes below the configured floor; any sensitive answer goes to `answer_profile_question`; client-supplied answers (and pay changes in human approval mode) apply only after `job-apply-mcp feedback --confirm <id>` on a terminal |

## Control layer and mail dedupe

| Tool | Inputs | Output | Notes |
|---|---|---|---|
| `control_check` | `action` (apply, submit, send_message, write_prose, merge), `payload` | `{decision: pass/block/needs_human, violations, warnings}` | Cheap dry check before a side effect. |
| `control_status` | none | mode, quotas used/remaining, `daily_limit` (limit, used in last 24h, remaining per scope; null = no limit), enabled controls, recent blocks | Read-only. |
| `ingest_confirmations` | `messages=[{sender,subject,date,snippet}]`, `preview=False` | counts plus up to `ingest.max_new_shown` new matches | You pass summaries from your own mail tool; the server never reads mail itself in this mode and stores only company, title, date. |
| `sync_confirmations` | `since_days=None`, `source=None` (mbox or imap), `preview=False` | counts plus a few new matches | Read-only; never sends, moves, flags or deletes mail. |
| `grants_status` | none | `{active:[{id,scope,expires_in_s,uses_left}]}` | Read-only view of human-issued grants (ADR-0027). Grants are created only by a person: `job-apply-mcp grant <scope> --for 2h` in a terminal. |
| `consent_status` | none | `{active:[{id,mode,scopes,expires_in_s,submissions_left,bound_to_this_run}],scopes}` | Read-only view of consent windows (ADR-0048). Only a person mints one: `job-apply-mcp consent --for 24h` or `--per-run` in a terminal. |
| `vault_status` | none | `{consent, auto_create, hosts:[{host,logins}], pending_verification, run_flags}` | ADR-0046. Read-only: which ATS hosts have a keychain login (count only, never a secret), whether the credentials consent window is open, accounts awaiting email verification, and this run's login flags (`lockout`, `wrong_password`, `captcha`, ...). Logins are added only by a person: `job-apply-mcp vault add <host> --username U`. |
| `reconcile_submissions` | `messages=None`, `force=False` | `{open, due, next_due_min, confirmed?, unresolved?, client_task?}` | ADR-0025. Matches open submissions (submitted/unknown/submitting/handoff) to confirmation emails by platform, company, title similarity and time window; a confident match moves the submission to `confirmed` (evidence: subject/date, never bodies). Checks are due `ingest.reconcile_delays_min` after each submit. Default client source returns a `client_task` to fetch summaries. Never resubmits. |
| `seed_from_confirmations` | `since_days=None` (180), `messages=None`, `preview=False` | like `sync_confirmations`, or `{client_task}` | Seeds dedupe from confirmation history before a run; no side files. |

## Text provenance

| Tool | Inputs | Output |
|---|---|---|
| `record_text` | `kind` (resume_bullet, cover_letter, answer, profile_fact), `text`, `origin` (user_written or llm_generated), `parent=None`, `model=None`, `prompt=None`, `context=None` | compact record with `id` |
| `verify_text` | `id`, `edited_text=None`, `verified_by="user"` | verified record (plus a diff when edited) |
| `provenance` | `id`, `include_text=False` | origin, model, prompt hash and ancestor chain |

## Style tuning (optional local LoRA)

| Tool | Inputs | Output |
|---|---|---|
| `tune_status` | none | backend, promoted and previous adapter, new user texts versus the threshold, last run |
| `tune_run` | `trigger` (manual, new_data, rollback), `wait=False` | run summary; trains a LoRA on your own texts, gates it against the incumbent and promotes only if better. `wait=False` runs in the background; poll `tune_status` |
| `profile_questions` | `limit` | top open profile gaps (questions left unresolved or answered with a default), ranked by frequency x impact; blocked applications weigh most (ADR-0047) |
| `answer_profile_question` | `key`, `scope`, `scope_value` | asks the USER (elicitation) to type the answer for a gap key; takes no value from the client; stored as `user_written`; nothing is stored if the client cannot elicit (ADR-0047) |
| `facts_report` | none | resume-first fact precedence: counts, profile/resume conflicts, and letter facts excluded as conflicting or outdated (with the resume's version) |

## Operations and self-improvement

| Tool | Inputs | Output |
|---|---|---|
| `health` | none | enabled platforms, LLM providers, browser drivers, `dry_run`, which local models answer |
| `improvement_report` | `limit=10`, `cursor=0` | ranked proposals learned from recent failures |
| `request_feature` | `description`, `kind=None` | a proposal; a PR only if `improve.coder` is configured (default `none`) |

## Recommended call order (lowest token cost)

1. `health` once per session.
2. `search_jobs` with a small `limit`, then `get_job` for the few that matter.
3. `plan_application` per finalist, answer its `client_tasks`.
4. `submit_application` with `token` (dry run first), or `apply_batch` for many.

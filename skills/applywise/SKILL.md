---
name: applywise
description: How to use the ApplyWise (job-apply) MCP tools safely to search for jobs, plan applications, dry-run them and hand real submits to the user for approval. Use when the user asks to find jobs, apply to a job or a batch of jobs, check application status, or set up ApplyWise.
---

# Using ApplyWise safely

ApplyWise is an MCP server (`plugin:applywise:applywise` in `/mcp`) that searches job boards and fills
application forms. It stops before anything irreversible. Your job is to drive the tools, keep the user in
control, and never work around a stop.

## Hard rules

These hold in every mode and every configuration. The server enforces them too; do not try to get around them.

- **Never enter a password, create an account, or sign in for the user.** If a site needs an account, tell the
  user and let them do it.
- **Never solve, click through or bypass a CAPTCHA or other bot check.** A CAPTCHA returns `needs_human` or
  `handoff`. Tell the user which page is open and that the final click is theirs.
- **Never read the user's mail to find a verification code.** When a submit returns `awaiting_code`, ask the user
  to type the code they received, then call `provide_verification_code` with it.
- **Never tick an attestation** that the server flagged (for example "I did not use AI"). It returns
  `needs_human`; the user decides.
- **Never invent facts.** Answers come from the user's profile and resume. If a required field is unresolved, ask
  the user rather than guessing.
- **Never run the human-only commands yourself**: `job-apply-mcp approve`, `deny`, `resolve` and `grant`. They are
  for the user in their own terminal. Print the exact command and let the user run it.
- **Never edit the user's config to loosen a safety setting** (`dry_run`, `approval_mode`, `auto_approve`,
  `captcha_policy`, daily limits, grant caps). Only the user changes those, in their own config file.

## 1. Set up and check (init, doctor)

On first use, call `health`. If it reports missing config or profile, ask the user to run these in their own
terminal (the plugin pins the server version; use the same pin shown in the plugin's `.mcp.json`):

```bash
uvx --from git+https://github.com/tanmayagrawal21/job-apply-mcp@7a2a1864916336fef2c8edfe3bd7aba5427d57ed job-apply-mcp init
uvx --from git+https://github.com/tanmayagrawal21/job-apply-mcp@7a2a1864916336fef2c8edfe3bd7aba5427d57ed job-apply-mcp doctor
```

`init` writes `config.toml`, `profile.toml` and `controls.toml` in the user's config directory and prints their
paths. The user fills in the profile themselves. Do not ask them to paste personal details into the chat so you
can write the file. `doctor` checks the setup and prints fixes. Run `/reload-plugins` or restart the session after
changes.

## 2. The normal flow: search, plan, dry run, review, approve

1. **Search.** `search_jobs` with a small `limit` (for example 10) and specific `boards` or a `query`. Page with
   the returned `next` cursor instead of raising the limit. Policy gates already drop obvious non-fits.
2. **Shortlist.** Call `get_job` only for the few postings that matter; ask for `"description"` only for
   finalists.
3. **Plan.** `plan_application` per finalist. It runs gates and the duplicate check and fills answers from the
   profile. Answer only its `client_tasks`, briefly and from the user's real facts. It returns an `approval_id`
   bound to a hash of the exact answers.
4. **Dry run.** `submit_application` without changing `dry_run`. The global `dry_run = true` default means nothing
   is sent. A per-call `dry_run=false` cannot override a global `dry_run=true`.
5. **Review.** Show the user the company, title and the answers, especially any free-text answers and cover
   letters. Let them edit. Changing an answer re-binds the approval, so the user re-approves what will be sent.
6. **Approve.** A real submit needs the user to have set `dry_run = false` in their own config, and an approval
   (see the next section). Then call `submit_application` with the `approval_id`.

For many postings, `apply_batch` does steps 3 to 6 in parallel and returns one row per ref. Rows that need input
come back as `needs_input` with `client_tasks`; none are skipped silently.

After submitting, `reconcile_submissions` matches open submissions to confirmation emails, and
`list_applications` shows the local record. A submission in `submitting` or `unknown` is never retried
automatically; ask the user what happened.

## 3. Approval modes: human and agent

- **`approval_mode = "human"` (default).** Every real submit needs a person. If the client supports MCP
  elicitation, the user sees a confirm dialog. Otherwise `submit_application` returns `needs_human` with a command
  such as `job-apply-mcp approve <id>`. Give the user that command; do not run it.
- **`approval_mode = "agent"`.** The agent may submit with the server-issued `approval_id`. Only the user can turn
  this on, and only in their own user config file; project files and environment variables cannot. Even in agent
  mode, CAPTCHAs, codes, account creation and flagged attestations still stop.

## 4. CAPTCHAs and handoff

The default `captcha_policy = "handoff"` fills and checks the form, then stops before the final click when the
page has any CAPTCHA widget. The status is `needs_human` or `handoff` and the tab stays open. Tell the user to
review the page and click submit themselves. `reconcile_submissions` then settles it from the confirmation
email, or the user records it with `job-apply-mcp resolve <ref> submitted` in their terminal.

`captcha_policy = "attempt"` is opt-in and is honoured only from the user's own config file. It clicks submit
once when the page has only an invisible widget and no visible challenge. It never solves a challenge. If the
user asks you to turn it on, explain this and let them edit their own config.

## 5. Grants

A grant lets the agent do one human step, such as approving submissions, for a limited time. Only a person can
create one, in an interactive terminal:

```bash
job-apply-mcp grant approve_submissions --for 2h --max-uses 5
```

It asks the user to type a confirmation sentence and cannot be scripted. You can only read grants with
`grants_status`. Do not ask for a grant to save time. Suggest one only when the user wants unattended runs, and
tell them what it allows.

## 6. Daily limits

Real submits are capped: 25 per rolling 24 hours, 10 per platform per 24 hours and 10 per server run, by
default. `control_status` shows what is used and what remains. When a cap is reached, stop and tell the user.
Lowering a cap works from any source. Raising or removing one (`0` means no limit) works only from the user's own
config file. Do not suggest raising a cap just to finish a batch.

## Keeping replies small

Tools return compact JSON. Use small `limit` values, request `fields` only when needed, and pass back short `ref`
values instead of full URLs or descriptions.

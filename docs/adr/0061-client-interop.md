# ADR-0061: Client interop: full question labels, resume readability, server instructions

Status: accepted · Date: 2026-10-27 · Amends ADR-0005, ADR-0023

## Context
Driving the tool from a non-Claude client showed three gaps:
1. Question labels reached the client truncated (`get_form` cut them at 80 chars, budget shaping at 60, and
   optional self-ID labels at 80). The client answered the wrong question.
2. The client could not use a resume under `~/Downloads`. On macOS, TCC denies the server process (a child of
   the client app) access to that folder, even though the user can open the file.
3. The server sent no `instructions`, so a new client did not know the flow or the rules.

## Decision
- **Labels.** `core.output.full_label` returns a label whole up to 500 chars. Past that it cuts the label and
  ends it with `…(truncated)`. `get_form` (`l`), client_tasks (`label`) and `skipped_voluntary` use it. Budget
  shaping never shortens a `label`/`l`/`q` string below that cap and never trims an `options`/`o` list (except
  in the last-resort `keep=0` pass). Options are returned up to 100 per question (`OPTIONS_MAX`) instead of 12
  or 30.
- **Resume access.** `resume_access.resolve_resume` checks that this process can open and read the resume. It
  reads one byte. If the configured `paths.resume` is unreadable, it falls back to a file with the same name in
  `<data_dir>/documents`. A relative `paths.resume` resolves there. If `paths.resume` is unset, it uses the
  single `resume.*` file there; with two or more it asks. The `health` tool reports `resume: {ok, path?, why?,
  fix?}`, and `doctor` reports a FAIL with the same fix: copy the file into `<data_dir>/documents`, or on macOS
  grant Full Disk Access to the client app. The profile view and `facts_report` use the resolved path.
- **Instructions.** `FastMCP("job-apply", instructions=INSTRUCTIONS)` (under 1,000 chars) gives the canonical
  flow: search/plan, ask the user every client_tasks question, submit with `approval_id` (optionally
  `final_click="human"`), confirm. It tells the client to follow `next_action`, never to retry an
  unknown/handoff submit, never to edit the tool's files or databases or fill forms outside the tool, and to
  report a wrong answer with `give_feedback` or `answer_profile_question`.

## Consequences
Responses grow a little for forms with long labels. The client sees the question the employer asked, and a
TCC-blocked resume has an actionable fix instead of a silent upload failure.

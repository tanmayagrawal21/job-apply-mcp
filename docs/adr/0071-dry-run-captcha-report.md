# ADR-0071: Dry-run results report the captcha the classifier sees

Status: accepted · Date: 2026-10-27 · Amends ADR-0059 (guidance) · Relates to ADR-0036, ADR-0048, ADR-0067

## Context
A browser dry run already classifies captcha widgets (ADR-0036) and writes them to the evidence JSON
(`captcha_widgets`), but the tool response (status, detail, hint, next_action) never mentioned them. A
`dry_run` with `next_action=show_user` read as "good to submit" even when a live submit would stop for the
user, so clients had to open evidence files to choose no-captcha forms for acceptance runs (issue #111).

## Decision
- The Playwright dry-run branch summarises the same `_captcha_state` (read-only) into `SubmitResult.captcha`
  (`[{kind, visible, enterprise}]`, values true/false/null) and `captcha_mode` (`none`, `implicit`,
  `challenge`, `unclear`); see `core/captcha_report.py`. `enterprise` comes from the host page's reCAPTCHA
  loader/frame `src` (vendor frames are never evaluated). An ATS whose live submit is always treated as
  captcha-protected (Ashby) is reported as `ats:ashby` / `unclear` when no widget is seen.
- `submit_application`, `apply_batch` (+`captcha_counts`) and `run_applications` / `run_report` (row `captcha`,
  report `captcha_counts`, stored in `intake_items.captcha`) carry it, client-agnostically.
- The dry-run `hint` says what a live submit would do: `implicit` is informational (one normal click only under
  `captcha_policy="attempt"` or a consent window, else the user clicks); `challenge` and `unclear` mean the user
  makes the final step. `next_action` stays `show_user`: nothing was sent and nothing is to be retried.
- Absent fields mean "not inspected" (API path, other drivers), never "no captcha".

## Consequences
Reporting only. Captcha detection, the live attempt/handoff rule, outcome classification and every browser
interaction are unchanged; the tool still never solves, evades or hides from a captcha (ADR-0036).

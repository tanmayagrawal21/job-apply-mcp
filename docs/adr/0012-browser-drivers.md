# ADR-0012: Browser drivers (Playwright deterministic, browser-use fallback)

Status: accepted · Date: 2026-10-22

## Context
Employers without API credentials are applied to through the web form. Real runs showed the same failure modes
repeatedly (react-select quirks, resume validation errors, CAPTCHAs, email codes, duplicate submits).

## Decision
- **`browser_playwright.PlaywrightDriver`** (extra `browser`, lazy import): deterministic fillers for Greenhouse,
  Lever and Ashby. One persistent context (profile dir `browser.user_data_dir`, optionally the installed Chrome
  via `browser.channel`) is shared by all jobs; one page per job. Images, fonts, media and analytics hosts are
  blocked via route interception (`browser.block_resources`, `block_hosts`). Fields are located by id/name, then
  accessible label; react-selects are driven by clicking the visible option (never Enter, which can submit);
  location lists wait for async options. Order: text, choices, resume upload LAST; then a read-back verify and one
  refill pass (parsers and uploads wipe fields); any unverified field -> `needs_human`.
  Exactly ONE real click on the single visible submit button. Success = confirmation URL, or confirmation text
  once the form is gone (extension overlays are ignored). No confirmation -> `needs_human` ("do NOT retry").
  CAPTCHA, verification code, login wall -> `roadblock`; stop phrases (`policy.stop_phrases`) in the form -> `needs_human`;
  "already applied / once every N days" -> `duplicate`. Waits are short polls with caps, not fixed sleeps.
  Unknown hosts return `needs_human` with detail `unsupported_site`, which makes the engine try the fallback.
- **`browser_use_driver.BrowserUseDriver`** (extra `agent`): generic LLM-driven fallback (browser-use) using the
  router's local model, `browser.agent_max_steps`, and a prompt carrying the same hard rules. Because an agent
  cannot be guaranteed to stop before submit, dry runs do not drive it unless `browser.agent_dry_run_fill`.
- Both self-register in `core.registry.browser_drivers`; the engine loads them lazily by `browser.driver` /
  `browser.fallback_driver`.

## Consequences
Greenhouse/Lever/Ashby run without any LLM tokens. React-select paths are covered by unit tests of the pure
helpers plus one live smoke test against a fixture form (native select, upload, confirmation URL); real board
layouts may need selector tuning.

## Verify / revert
Offline: `uv run pytest tests/test_apply_browser_pure.py`. Live: `uv sync --extra browser` then
`JAM_LIVE=1 BROWSER_CHANNEL=chrome uv run pytest tests/test_apply_browser_live.py`. Revert: delete the two driver modules and the extras.

## Amendment 1: evidence, confirmation detection, pre-submit validation (fix/browser-evidence)
Context: two real Ashby submits (3 and 5 fields) ended "submit clicked but no confirmation detected" and no email
arrived, so the outcome was unknown. A manual submit of the same form later worked. Ashby has no `<form>` element
around its controls (buttons are "outside" any form), carries an invisible reCAPTCHA, autosaves via
`ApiSetFormValue`, and its React state needs real key events (a fixture reproduces a controlled input that ignores
programmatic sets).
Decision:
- **Evidence** on every dry run, pre-submit failure, live submit and non-success outcome, under
  `<paths.data_dir>/evidence/<sha256(ref)[:12]>/<stage>-<ts>.{png,json}`: full-page screenshot, trimmed visible text,
  URL, visible validation errors (`aria-invalid`, `role=alert`, banner/alert/error classes), POST responses (status
  + short body for ATS submit endpoints, status only for others), reCAPTCHA/hCaptcha/Turnstile presence and a
  button summary. `SubmitResult.detail` carries `evidence=<dir>` and field keys only, never values. Knobs:
  `browser.evidence`, `evidence_text_chars`, `evidence_body_chars`.
- **Detection**: `decide_outcome` prefers the network response (Ashby `ApiSubmit*` mutation: success typename /
  no `errors`; Greenhouse/Lever POST status), then URL (`/confirmation`, `/thanks`), then success text only once the
  form is gone. Polling up to `confirmation_wait_s`. No success signal is reported as FAILED/not submitted
  (`needs_human`, never retried) with the captured errors.
- **Pre-submit validation** (`browser.presubmit_validation`): after fill + read-back (now including Yes/No state via
  `aria-pressed`/`aria-checked`/radio and location input values) any visible validation error returns `needs_human`
  without clicking.
- **Ashby**: keyboard typing plus blur; Yes/No need a real click confirmed by `aria-pressed` (one retry); location
  requires a chosen suggestion; submit waits for the uploaded filename chip.
- **Submit button**: prefers visible+enabled buttons named Submit/Submit application inside the `<form>`, then
  anywhere by name; still refuses if more than one candidate remains.
Verify: `uv run pytest tests/test_apply_browser_local.py` (needs local Chrome, skipped otherwise). Tests always use a
tmp `data_dir` (autouse fixture in `tests/conftest.py`). Revert: revert the PR; config knobs are additive.

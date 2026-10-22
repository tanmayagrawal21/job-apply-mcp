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
`BROWSER_CHANNEL=chrome uv run pytest -m live`. Revert: delete the two driver modules and the extras.

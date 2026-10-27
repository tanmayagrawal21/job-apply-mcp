# ADR-0060: Per-call fill-only mode: `submit_application(final_click="human")`

Status: accepted · Date: 2026-10-27 · Amends ADR-0058

## Context
ADR-0058 made "the person clicks Submit" a config setting. A client sometimes needs this for a single
application: the user wants to look before sending, or the site is protected by reCAPTCHA Enterprise. It
should not need to edit the config to get it.

## Decision
- `submit_application` takes `final_click: "human" | "agent" | None`. `"human"` sets the `FINAL_CLICK` context
  variable (`apply/ports.py`) for the duration of that call only. `final_click(configured)` returns `"human"`
  when either the config or the call asks for it.
- **Tighten-only.** A per-call `"agent"` is accepted and has no effect, so a configured `"human"` always holds.
  Any other value is `invalid_input`.
- When the effective mode is `"human"` in a live run, the API submit path is skipped, because an API submit has
  no click a person could make. The skip applies to the configured mode too, which closes a gap in ADR-0058.
  Browser drivers that cannot hand off are skipped as well (they have no `on_human_confirmed`, e.g. the generic
  browser-use agent). The Playwright driver fills, verifies, keeps the tab and returns `handoff`. The ADR-0058
  watcher records the person's submission through the normal state machine. Headless mode stays `needs_human`.
- Dry runs are unchanged.

## Consequences
Any client can ask for a fill-only submission without touching the config, and it can never use this parameter
to loosen a configured human click.

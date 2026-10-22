# ADR-0014: Control layer (last line of defense)

Status: accepted · Date: 2026-10-22

## Context
Applying, sending messages and merging are side effects. Prompts and per-component checks can be
bypassed by bugs, prompt injection in scraped pages, or a misconfigured run. We need one generic,
user-configurable gate that every side-effecting action passes through, with an audit trail.

## Decision
- `control/` package. A **Control** = `(id, applies_to ⊆ {apply, submit, send_message, write_prose, merge},
  check(ctx) -> [Violation], severity block|warn)`. Built-ins self-register in a local `Registry`.
- **Config** (`[control]` in config.py): `enabled` ids, per-control `params`, `apply_enabled`, `dev_mode`,
  `rules_file`, `audit`, `review_*`. Users add rules declaratively in a **TOML rules file outside the repo**
  (`examples/controls.example.toml`): `[mode]`, `[params.<id>]`, `[[rule]]` of type `phrases_banned`,
  `phrases_required`, `regex`, `threshold`, `allow_list`, `deny_list`, `quota`. Invalid files fail closed
  (every check blocks, `control_status` shows `config_error`).
- **Built-ins** (all generic; lexicons are overridable defaults): `mode` (apply_enabled / dev_mode / global
  dry_run), `quota` (day/run/platform), `attestation` (policy.stop_phrases + extras -> needs_human),
  `hard_stop` (captcha, verification code, account/password creation, OAuth consent -> needs_human),
  `single_submit` (same ref within N minutes), `prose_lint`, `recipient_deny`, and `page_text_injection`
  (warn) backed by `sanitize_untrusted()`, which strips AI-addressed sentences and invisible characters.
- **ControlAgent.check(action, ctx) -> ControlReport{pass|block|needs_human, violations, warnings}**.
  needs_human only if every blocking violation demands a human; otherwise block. Deterministic controls
  run first; the optional model review (router task_class `review`, via `make_llm_reviewer`) runs only when
  they found nothing blocking and `review_enabled` is set, and its issues are warnings (reviewer errors:
  warn, or needs_human via `review_fail`).
- **Usage state** (quotas, idempotency) counts only passed, live (non-dry-run) side-effecting checks, is
  appended to `<data_dir>/control_audit.jsonl` (one compact row per decision, no prose bodies) and replayed
  from it on start (2-day window), so limits survive restarts. Checks are serialized by a lock so parallel
  submits cannot exceed a quota.
- **MCP tools** `control_check(action, payload)` and `control_status()` (mode, quotas used/left, controls,
  recent blocks), registered with one line in server.py.

## Integration note for apply/engine
Create one `ControlAgent` per process (`control.tools.get_agent(settings)`). Immediately before any
irreversible step call:

    rep = await agent.check("submit", {"ref": ref, "platform": p, "company": c, "dry_run": dry,
                                       "page_text": text, "form_labels": labels, "attestations": [...]})
    if rep.decision != "pass": return result(status=rep.decision, reasons=rep.compact())  # never retry or bypass

Also call it with `"apply"` before filling, `"write_prose"` with `{"text": ...}` on every generated essay
(re-generate on block; escalate on `needs_human`), `"send_message"` with `{"recipient", "text"}`. Pass scraped
text through `agent.sanitize(text).text` before any LLM call. `needs_human` means stop and surface to the
user; do not solve CAPTCHAs, create accounts, enter codes, or tick attestations.
The `ref` passed to submit is what idempotency keys on. A passing live `submit` check consumes quota, so call
it once per actual submission.

## Consequences
Extra latency is negligible (regex only). Defaults are conservative (quota 25/day, 10/run). Heuristic
detectors can false-positive; they err toward needs_human and are tunable or disable-able per control.
The sanitizer is defense in depth, not a guarantee.

## Verify / revert
`uv run pytest tests/test_control.py`. Revert: drop `control/`, the `ControlCfg` block in config.py and the
two server.py lines; nothing else depends on them.

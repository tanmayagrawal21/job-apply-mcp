# ADR-0032: Quota release for submits that sent nothing

Status: accepted · Date: 2026-10-22 · Amends ADR-0014

## Context
`ControlAgent.check("submit")` adds a usage event for every live submit that passes the controls, before the
click. Fill failures, results returned before the click, and pre-dispatch errors therefore consumed the
day, platform and run quotas (and the `single_submit` window) although nothing was sent.

## Decision
- `ControlAgent.release(ref, reason)` appends a `submit_release` usage event and an audit row (same row format
  plus `reason`; `plat`/`co` copied from the ref's latest submit).
- `UsageState.count` and `seen_ref` ignore a `submit` event when a later `submit_release` exists for the
  same ref. Replay rebuilds both events from the audit log, so releases survive restarts.
- `ApplyEngine` calls `control.release` after a live submit only when the approvals state is in
  `_NOT_SENT` (`planned`, `approved`, `failed`; the constant from control/builtin.py), including pre-dispatch
  exceptions. Never for submitting, unknown, submitted, confirmed, handoff or awaiting_code. A control object
  without `release` is skipped; a failing release is swallowed (the count simply stays higher: fail closed).

## Consequences
Quotas count only attempts that may have reached the platform. Ambiguous outcomes keep consuming quota.
Release rows are not shown in `recent_blocks`.

## Verify / revert
`uv run pytest tests/test_quota_release.py`. Revert: remove `release`, the `_live` filter in `UsageState`,
and `_release_unsent` calls in apply/engine.py.

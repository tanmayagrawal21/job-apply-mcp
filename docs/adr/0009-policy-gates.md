# ADR-0009: Policy gates

Status: accepted · Date: 2026-10-22

## Context
Applying blindly wastes effort and risks false attestations. Filters (experience, graduation window,
sponsorship, citizenship/clearance/ITAR, pay, employer type) differ per user, so they must be configurable,
and they must be testable without network or LLMs.

## Decision
- `policy/gates.py`: each gate is a pure function `(GateContext, PolicyCfg) -> (Decision, reason) | None`,
  registered in the ordered dict `GATES`. `policy.gates` in config enables, disables or reorders them
  (default: all; unknown names raise). `register_gate()` adds custom ones.
- Combination: any `skip` wins; otherwise any `needs_human`; otherwise `submit`. Reasons are short strings.
- Regex heuristics over posting text; false negatives (missed filter) are preferred to false positives
  (wrongly skipping), except stop phrases, which always escalate to `needs_human` (safety invariant).
- Salary compares the *top* of the posted range to `salary_floor * col_multiplier` (first matching
  location substring); hourly or tiny figures are annualized with `hours_per_year`. No posted pay passes.
- Citizenship: `us_citizen`, `us_person`, `has_clearance` candidate facts; "ability to obtain a clearance"
  passes for citizens.
- Flags from the shared text-flagging helper (`Posting.flags`) are merged in; a local fallback
  (`local_flags`) means no dependency on the platforms package.
- New `PolicyCfg` fields: `gates, us_citizen, us_person, has_clearance, graduation_flex_months, hours_per_year`.

## Consequences
Heuristics can misread unusual phrasing; users tune via config or add a gate. Gate order only affects the
reason list order, not the decision.

## Verify / revert
`uv run pytest tests/test_gates.py -q`. Revert: remove `policy/gates.py` and the new `PolicyCfg` fields.

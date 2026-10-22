# ADR-0018: Privacy guard

Status: accepted · Date: 2026-10-22

## Context
The repo is public and built from a real user's workflow. Personal data (emails, phones, addresses, resumes,
home paths) and secrets must never land in git history, and private identifiers (names, employers) cannot be
listed in a public repo for CI to match.

## Decision
- `scripts/privacy_scan.py` (stdlib only): scans added lines (`--diff`, `--staged`), given paths, or the tree.
  Detects emails (except example/test/invalid domains), phones, street addresses, secrets (sk-, ghp_/github_pat_,
  AKIA, xox, private-key headers, high-entropy key=value), person-data file types outside tests/fixtures/examples,
  and absolute home paths. Exit 1 on findings.
- Three layers: (1) CI job, public patterns only, `--diff origin/<base>`; (2) local optional pre-commit hook
  (`scripts/install-hooks.sh`) adding `--terms ~/.config/job-apply-mcp/privacy_terms.txt`, whose terms are
  never printed (findings say "private term #k" with a masked excerpt); (3) a privacy agent that reviews
  each PR alongside the reviewer agent, using the local terms file, before merge.
- `.privacy-allow` holds regexes for fictional fixtures; entries need review like code.

## Consequences
Heuristics yield false positives (allowlist) and false negatives; private terms only protect the local machine
and the privacy agent's run, not CI. Line-level allowlisting suppresses all kinds on a matched line.

## Verify / revert
`python scripts/privacy_scan.py` (whole tree) and `uv run pytest tests/test_privacy_scan.py -q`.
Revert: delete the script, hook installer, `.privacy-allow`, the CI `privacy` job and this ADR.

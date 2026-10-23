# ADR-0051: Accepted findings for the privacy scan

Status: accepted · Date: 2026-10-22

## Context
A full-tree scan with the owner's private terms always exits 1. Every finding is known and accepted: the owner's
public GitHub handle inside repository URLs, and the owner's name in `LICENSE` (kept by owner decision,
2026-10-22). A scan that always fails hides real new leaks. Listing the accepted values in the repo would publish
the very terms the scan protects.

## Decision
1. `scripts/privacy_scan.py --allow FILE`, defaulting to `~/.config/job-apply-mcp/privacy_allow.txt` when it
   exists. The file lives outside the repo and is never committed.
2. Lines: `@salt <random>`; `<path-glob><TAB><fingerprint-or-*>`; or `@urls`. A fingerprint is the first 12 hex
   characters of SHA-256 over the salt and the lower-cased term, printed by `--fingerprint K` for term #K. Rules
   follow the term, not its position, so reordering the terms file does not shift them, and the file holds no
   value. Blank lines and `#` comments are ignored; malformed lines are skipped with a note that does not echo
   the line.
3. `@urls` accepts a term only when its match lies inside the owner segment of `https://github.com/<owner>[/...]`
   (the owner's private terms can be parts of the handle, e.g. a first name). A term in a repo name, path, query
   string or fragment stays a finding.
4. Rules accept only private-term findings. Emails, phones, addresses, home paths and secrets are never accepted.
5. Acceptance applies to whole-tree scans only. `--diff` and `--staged` (PRs, pre-commit) report every finding on
   an added line, so a reviewer sees each new occurrence.
6. An accepted finding is counted (`N accepted`) and not printed. Exit 0 only when every finding is accepted.
   With no allow file, behaviour is unchanged.
7. Term values and matched text are never printed in any mode. Overlapping masked spans are merged, so an
   excerpt cannot leak the tail of a longer match.

## Consequences
The full scan is a meaningful gate again on the owner's machine. The allow file names only paths, a salt and
fingerprints, so it holds no personal value. CI, which has no terms file, is unaffected. The in-repo `.privacy-allow` (regexes for
fictional fixtures) keeps its role.

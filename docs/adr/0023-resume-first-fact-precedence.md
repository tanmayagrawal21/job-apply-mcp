# ADR-0023: Fact precedence (resume-first), writing lint gaps, quality gate, cover-letter policy

Status: accepted · Date: 2026-10-22

## Context
A real comparison showed drafts built from older letters: wrong addressee, placeholders, "currently completing"
a degree already finished, looping/markdown garbage, and generic prose weaker than the user's own letters.
Owner rule: the resume is the source of truth for facts and timelines; user letters may add content the resume
lacks but must never override or lag it.

## Decisions
1. **Precedence.** resume (1, `origin=user_written, source=resume`) > profile facts and non-conflicting user-letter
   facts (2) > everything else (never used). `profile.py` became the package `profile/` (imports unchanged);
   `profile/resume.py` parses PDF (optional extra `job-apply-mcp[resume]`, lazy `pypdf`) or .txt/.md text into
   `{section, org, title, start, end/current, bullets, metrics}`; `profile/facts.py` resolves precedence.
2. **Letter facts** are admitted only if they do not contradict a resume fact (org/title, date span, metric, status).
   Conflicts (older title, dates outside the resume span, "currently pursuing" when the degree end date is past,
   different metric for the same claim) are excluded and reported with reason and the resume's version.
   Profile entries for orgs the resume covers are superseded by the resume.
3. **Writers get only `FactSet.lines`** (`ProfileView.facts`), plus TIMELINE tense guidance derived from resume
   dates (current role = no end date). The exemplar is passed as "style only — do not copy facts, names,
   addressees, dates". Engine edit is limited to passing `resume`/`corpus` to `write_text` and the scorecard to tasks.
4. **`resume_consistency()`** (profile vs resume) is exposed as the `doctor` check `resume_facts` and the compact
   tool `facts_report`.
5. **New lint violations** (`writing/checks.py`): placeholder, addressee, temporal, degenerate (repeated sentence,
   4-gram loop, markdown/image syntax, >`degenerate_max_nonalpha` non-alphabetic tokens), closing
   (`writing.required_closing` regexes), leakage (exemplar addressees/orgs/full dates); inflation phrases added to
   `banned_phrases`. A draft with any violation can never be `pass`.
6. **Quality gate** (`writing/quality.py`). Deterministic metrics for the draft and the user's corpus baseline
   (mean/sd; corpus = exemplar + `writing.style_corpus` + user-trusted cover letters in provenance): evidence
   density (grounded in resume-first facts), company-problem connection (fixed floor, since letters have no
   posting), structure fidelity, specificity, concision. Pass needs metrics >= baseline - `quality_tolerance`*sd
   (min 20% of the mean). Reviewer rubric (client task by default, or `reviewer="router"`) scores argument, voice,
   precision, specificity 1-5 against `quality_min_scores` with 1-2 of the user's letters as a style-only bar and
   must answer "at least as strong as the reference letters?" with sentence-level edits. Failures feed the revise
   loop; after `max_rounds` the result is `needs_human` with the scorecard (also in the plan's client task).
7. **Tuning gate**: degeneration and exemplar-leakage in held-out generations force REJECT regardless of loss.
8. **`writing.cover_letter_policy`**: `required_only` (default; optional cover-letter fields, file or text, are
   skipped entirely: no draft, no client task), `always`, `never` (required ones become a client task without a draft).

## Consequences
Drafts follow the resume's timeline; stale letter content is quarantined with an audit trail. More rejections for
weak prose, by design. Resume parsing is heuristic: use clear headings and date ranges; `doctor` warns when nothing parses.

## Verify / revert
`uv run pytest tests/test_resume_first.py`; `job-apply-mcp doctor`; tool `facts_report`. Revert: set
`writing.quality_gate=false`, `cover_letter_policy="always"`, leave `paths.resume` unset (falls back to profile
facts); or revert the PR (profile/ package rename is import-compatible).

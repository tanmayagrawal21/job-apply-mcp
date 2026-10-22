# ADR-0013: Two-stage writing pipeline (writer, lint, strict reviewer)

Status: accepted · Date: 2026-10-22

## Context
Cover letters and long answers must be truthful and restrained: no invented claims, no stated gaps, no
manufactured enthusiasm or flattery, formal register. Small local models drift on all of these.

## Decision
`apply/writing.py`: **writer** (`task_class="narrative"`, default local gemma via Ollama) gets a compact posting
summary (the company's stated problem and the role's duties), profile facts, the configured rules and an optional
style exemplar (`writing.style_exemplar`, outside the repo; structure and tone only). A deterministic **lint**
runs first (banned-phrase lexicon, contractions, gap patterns such as "I have not", "I lack", "I would need to learn",
paragraph count, length, company name present, 8-word overlap with the exemplar). Lint failures go back to the writer
as a revision request; no reviewer tokens are spent on them. Once lint is clean the **strict reviewer**
(`task_class="review"`) runs: with `writing.reviewer="client"` (default) the draft is returned as a client_task with
the rubric for the calling agent (status `needs_review`); with `"router"` the configured review model returns JSON
`{pass, violations, corrected}` and the loop continues up to `writing.max_rounds`, then `needs_human`.
Everything (rules, lexicon, patterns, lengths, rounds) is in `WritingCfg`. The engine runs it only for required
long text, concurrently across fields and jobs, and only when the narrative tier is locally available.

## Consequences
Deterministic checks cannot prove factual support; that is the reviewer's job, hence the default hand-off to the
calling agent. The lexicon may over-flag (for example "love to"); tune in config.

## Verify / revert
`uv run pytest tests/test_apply_writing.py`. Revert: set `writing.enabled=false` (long text becomes client_tasks) or
delete `writing.py`.

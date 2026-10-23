# ADR-0057: Required essays: client path first, optional local autodraft, strict local review

Status: accepted · Date: 2026-10-23 · Builds on ADR-0013 (prose pipeline), ADR-0041 (prose budget, deferred
drafting), ADR-0044 (fail-closed review), ADR-0045 (run_applications), ADR-0052 (autonomy level)

## Context
QA on 22 real postings found that required essay fields ("Why do you want to work here?", "Describe a project…")
are the largest blocker for unattended runs: 8 unresolved. The owner chose `writing.reviewer = "router"` ("end-to-end
automatic; a local strict reviewer approves prose") and `answers.autonomy = "infer"`, but essays never got
filled. There were four causes:

1. **The run path never drafted.** `run_applications` plans with `writing.mode = "deferred"` (ADR-0045). Prose
   becomes a `deferred` client task, and the runner then skipped the posting ("required prose not approved yet").
   Nothing in an unattended run ever called `draft_answers`.
2. **The "router" reviewer was not local.** The default `llm.task_tiers.review` is the `client` provider. With
   `allow_cloud = true` the router's review call raised `ClientTaskRequired`, `RouterView.complete` turned that into
   `ComponentMissing`, and `_write_long` dropped the field into `rest`. So the essay stayed unresolved with no
   draft at all.
3. **The writer missed the budget inline.** The default `narrative` tier is `gemma4:12b-it-qat` (heavy, 150 s
   per-call timeout). Inside `writing.timeout_s = 90` on a 16 GB machine, `plan_application` spent 78-91 s and
   returned `timed_out` tasks (QA timings for Harvey/Cohere).
4. **One task tier could not be changed alone.** Setting `[llm.task_tiers.review]` in TOML replaced the whole
   `task_tiers` table, which dropped `narrative`. So the owner could not simply point the reviewer at a local model.

## Decision
1. **Bug fixes (always on).**
   - Essays are no longer judged by cover-letter lint. `lint(essay=True)` skips the paragraph count, the company-name
     requirement, the addressee and the closing for every non-cover-letter text, in both `write_text` and
     `review_text`. A 700-character essay used to fail "3 paragraphs; want 4-6" and "company never appears" on every
     round.
   - The strict reviewer now works on short essays. It returns a verdict and findings only (`{"pass","violations"}`,
     JSON schema), so a local model answers in about 30-45 s instead of rewriting the text. It sees 40 facts, not
     12, so true claims are not flagged as unsupported. A plan ("how I would approach it") is not a claim.
   - A client-written essay is reviewed by the LOCAL strict reviewer. In `submit_application`, ADR-0044 review of a
     non-cover-letter text uses the local-only `essay_review` class. With the default `review` tier (provider
     `client`), the review used to hand the task back to the client and fail closed (`ComponentMissing`) every time.
     The checks also include `employer_claims`: the essay must not say the candidate worked at the posting's company.
   - Deferred essays are no longer swallowed silently. See 2.
2. **The client path is first-class for unattended runs.** `run_applications` now returns a top-level
   `client_tasks` map: `{ref: {approval_id, essays: [{key, label, required, max_words, max_len, prompt, facts}]}}`.
   It covers every unresolved essay, whether the posting was skipped as prose-only or needs the owner for other
   fields. The MCP client drafts each essay from the prompt (grounding facts, word limit, rules) and hands it back
   with `submit_application(ref, answers={key: text}, approval_id, dry_run)`. There the essay goes through the
   deterministic checks and the local strict reviewer (ADR-0044). It returns `needs_review` with findings when it
   fails. Rejected local drafts are never put in `client_tasks`.
3. **Local autodraft: optional and OFF by default.** It needs the new `writing.local_essays = true` together with
   `writing.reviewer = "router"`, both from the TRUSTED user config, and `answers.autonomy` safe_default or infer
   (`Settings.essay_autodraft`). It also stays off when a project file or `JOB_APPLY__*` changed anything it
   relies on (`config._autodraft_view`: reviewer, `local_essays`, the prose rules, the exemplar and corpus,
   `timeout_s`, the essay tiers and their fallbacks, `local_only_classes`, `allow_cloud`, providers, system
   prefixes). The flag is private, so only `load_settings` sets it, and an env/project/untrusted file never
   enables it. When it is on:
   - the `essay` tier writes, then lint, `fact_conflicts`, grounding of numbers and names, and `employer_claims`
     run, then the `essay_review` tier gives the verdict, for up to `max_rounds`, under `writing.timeout_s`;
   - a pass becomes an answer labelled `assumed` (`source="assumed:essay_autodraft"`, rationale with the saved file)
     in the plan, `audit`, evidence `assumed.json` and the run report;
   - a fail, error, timeout or missing local model leaves a required essay unresolved (`needs_human`) and an
     optional one blank;
   - every draft is saved at `<data_dir>/evidence/<sha12(ref)>/essays/<key>.md`;
   - optional essays are drafted only under `infer`;
   - in `run_applications`, prose-only postings are drafted one at a time, in priority order, while they could
     still take a slot.
4. **Local-only classes.** `essay` and `essay_review` are listed in `llm.local_only_classes`. They skip cloud and
   client candidates, never escalate and never use `client_on_failure`. A missing tier fails closed. Essay text
   reviewed or written by these classes never leaves the machine.
5. **Per-tier model choice.** `[llm.task_tiers.<class>]` / `[llm.fallbacks.<class>]` now change one tier. Fields
   left out keep their defaults, and the other tiers stay. For example `[llm.task_tiers.essay_review]
   model = "qwen2.5:7b"`.
6. **Cover letters** are unchanged.

## Model choice (benchmark, 2026-10-23, 16 GB Apple silicon, Ollama, 3 essays, same model as writer and reviewer)
| model | passed | latency per essay |
|---|---|---|
| qwen2.5:7b | 0/3 (both runs) | 77-90 s; as reviewer it returned invalid JSON or wrong findings (it quoted the rules back) |
| gemma4:12b-it-qat | 1/3 per run | 109-180 s; one essay timed out at 180 s |
| qwen2.5:7b writer + gemma4:12b reviewer | 0/3 | 97-151 s; the weight swap made the reviewer time out |

The local autodraft is not viable for unattended runs on this machine: 1/3 at 109-180 s, against a 90 s budget.
Following the owner's principle (prune "plus" work that fails or is slow), it stays OFF by default. The client path
is the unattended default. The strict reviewer defaults to `gemma4:12b-it-qat`, which reviewed a client essay in
33-45 s warm (more than 90 s cold). That is the one local step on the client path.

## Consequences
- With the client path, an unattended run (Claude as the client) can fill required essays. The only text that is filled
  passed the deterministic checks and the local strict reviewer. The local autodraft remains available for owners
  with faster hardware.
- A weak local reviewer fails closed: invalid JSON, a timeout or a missing model leaves the field unresolved. A
  model that is too lenient is backstopped by the deterministic grounding and lint checks, but not completely. The
  owner reviews the saved files, and repeated rejections show up as `needs_human` rows.
- Grounding is strict, so the check can reject good text (for example a proper noun the facts spell differently).
  That costs a `needs_human` row, never a wrong submission.

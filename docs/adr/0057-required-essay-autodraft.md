# ADR-0057: Required essays: local autodraft, local strict review, fill only on a pass

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
1. **Gate (`Settings.essay_autodraft`).** The autodraft is on only when all of these hold:
   - the TRUSTED user config itself sets `writing.reviewer = "router"`;
   - the final `answers.autonomy` is `safe_default` or `infer`. Autonomy can only get stricter outside the trusted
     file (ADR-0052), so the environment can still turn the autodraft off;
   - nothing outside the trusted file changed what the autodraft relies on (`config._autodraft_view`): the reviewer
     choice, the prose rules, banned phrases and gap patterns, `max_rounds`, the exemplar and corpus,
     `writing.timeout_s`, the `essay` / `essay_review` tiers and their fallbacks, `llm.local_only_classes`,
     `allow_cloud`, the provider endpoints and the system prefixes.

   The flag is a private attribute that only `load_settings` sets, like `_otp_mail_trusted` (ADR-0048). A project
   file, `JOB_APPLY__*`, a config chosen with `JOB_APPLY_MCP_CONFIG` or a group/world-writable file never enables it.
   With `reviewer = "client"`, `autonomy = "strict"` or an untrusted source, behaviour is unchanged.
2. **Local-only task classes.** There are two new task tiers. `essay` is the writer and `essay_review` is the strict
   reviewer, and both default to `ollama:qwen2.5:7b` with no fallbacks. They are listed in the new
   `llm.local_only_classes`. For these classes the router skips cloud and `client` candidates, never escalates to
   another tier and never applies `client_on_failure`. A local-only class with no configured tier fails closed. So
   essay prose never reaches a cloud provider, whatever `allow_cloud` says. Both classes are in `heavy_classes`, so
   on a 16 GB machine they run one at a time.
3. **Per-tier model choice.** `[llm.task_tiers.<class>]` and `[llm.fallbacks.<class>]` now change one tier. Missing
   fields keep that tier's defaults, and the other tiers stay as they are. For example
   `[llm.task_tiers.essay_review] model = "gemma4:12b-it-qat"` is a complete setting. `tiers` (small/medium/large)
   is unchanged.
4. **Pipeline per essay** (a non-cover-letter prose question that the existing `_is_prose_question` and
   `_llm_allowed` gates accept):
   - The **writer** (`essay`) uses the existing prompt: the precedence-resolved facts (resume, then profile and
     persona), the posting summary, the owner's style exemplar, the rules and the timeline.
   - **Deterministic checks**, before any review tokens are spent: lint (banned enthusiasm phrases, gap
     statements, contractions, placeholders, temporal), `fact_conflicts` against the resume, and the new grounding
     checks. Every number and every capitalised name must appear in the facts, the posting or the question. The
     essay must not claim that the candidate works or worked at the company being applied to.
   - The **strict reviewer** (`essay_review`) returns a verdict and findings only: `{"pass", "violations"}`, under a
     JSON schema, with no rewrite. A small model answers that quickly and as valid JSON. The draft is fenced as
     untrusted data (ADR-0044). It fails on unsupported claims, an implied current employer, gaps, enthusiasm or
     flattery, not answering the question, letter form, contractions and attestations. A pass needs
     `pass: true` with no violations. Findings go back to the writer for up to `writing.max_rounds`.
   - All of this runs under `writing.timeout_s` for the plan (ADR-0041).
5. **Outcome.**
   - **Pass**: the essay becomes an answer with `source = "assumed:essay_autodraft"`, `assumed = True` and a rationale
     that names the saved file. It appears in the plan (`assumed`, shortened to 120 chars), in `audit.assumed`, in the
     evidence `assumed.json` (merged when `draft_answers` adds it) and in the run report (`assumed`, shortened).
   - **Fail, error, timeout or no local model**: a required essay stays unresolved as a `needs_human` client task
     with the reason and findings. The rejected text is NOT in the task or the plan. An optional essay stays blank.
   - Every draft, approved or rejected, is saved in full at `<data_dir>/evidence/<sha12(ref)>/essays/<key>.md`.
6. **Optional essays** are drafted only under `infer`. Under `safe_default` they stay blank, and with the autodraft
   on, `apply.write_optional` does not list them either.
7. **run_applications.** After the deferred planning pass, postings whose only open fields are deferred prose are
   drafted (`engine.draft`) one at a time, in priority order, only while they could still take a slot under
   `max_applications`. Each one gets the full `writing.timeout_s`. A drafted posting is classified again: ready if
   every essay passed, otherwise `needs_human`. Postings left over keep the old "prose not approved yet" skip.
8. **Cover letters** are out of scope. They keep the existing narrative pipeline, quality gate and reviewer setting.

## Model choice (benchmark, 2026-10-23, 16 GB Apple silicon, Ollama)
See the PR description for the 3-essay benchmark (qwen2.5:7b vs gemma4:12b-it-qat, the same model as writer and
reviewer): latency per essay, pass rate and findings. The default is `qwen2.5:7b` for both tiers. It fits
alongside the resident small model, it keeps the writer and reviewer on one set of weights (no swap between
calls), and it finishes inside the default 90 s budget. Owners with more memory or time can set either tier to
`gemma4:12b-it-qat`.

## Consequences
- An unattended run can fill required essays without the owner, but only with text that passed every deterministic
  check and a local strict reviewer. Each one is labelled as assumed and saved for the owner to read.
- A weak local reviewer fails closed: invalid JSON, a timeout or a missing model leaves the field unresolved. A
  model that is too lenient is backstopped by the deterministic grounding and lint checks, but not completely. The
  owner reviews the saved files, and repeated rejections show up as `needs_human` rows.
- Grounding is strict, so the check can reject good text (for example a proper noun the facts spell differently).
  That costs a `needs_human` row, never a wrong submission.

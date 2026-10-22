# ADR-0017: Continuous local style tuning (LoRA)

Status: accepted · Date: 2026-10-22

## Context
Owner-approved: adapt a LOCAL open model to the end user's own writing style, continuously, without sending
text anywhere. Training data must be authored or approved by the user (ADR-0007 provenance), never raw LLM output.

## Decisions
1. **Data only via `DatasetSource`** (`tuning/source.py`). Codes against ADR-0007's Protocol (PR #7); if
   `job_apply_mcp.provenance` is absent, a structurally identical local fallback is used. `trusted_items()` re-filters
   to `user_written|user_verified|user_edited` as defense in depth, so `llm_generated` text cannot enter a dataset.
2. **Dataset builder** (`dataset.py`): SFT chat pairs (instruction = kind + compact `context` + compact facts from
   `profile_fact` records; output = the user's text). Normalised-text/3-gram-Jaccard dedupe, word-length filters,
   deterministic hash-based train/eval split (held-out set is stable as data grows), JSONL in
   `<data_dir>/tuning/<run_id>/`. **Preference pairs** (`preferences.jsonl`, rejected = LLM draft, chosen = user edit):
   the draft is not reachable through `DatasetSource`, so it is reconstructed by reverse-applying
   `diff_from_parent`; unrecoverable diffs are skipped. mlx-lm has no DPO trainer, so the MLX backend trains on
   SFT only; preference data is written for backends that can use it. Optional `paragraph_split` makes one example per
   paragraph for tiny corpora (weak: eval paragraphs share the author/letter with training).
3. **Trainer Protocol** (`trainer.py`; `available/train/generate/nll`) with a name registry; the **MLX** backend
   (macOS/Apple silicon) shells out to `python -m mlx_lm lora` (isolated memory, killable for the wall-clock budget,
   last periodic checkpoint kept), with `--grad-checkpoint`, batch 1, `--mask-prompt`. mlx-lm is lazily imported and
   lives in the optional extra `tune`. Default base `mlx-community/Qwen3-4B-4bit` (fits 16 GB; configurable).
4. **Eval gate** (`evalgate.py`): candidate vs incumbent (promoted adapter, else base) on held-out user texts:
   held-out NLL/perplexity, style-lint violations per 100 words (reuses `control.builtin` contraction, gap and
   enthusiasm lexicons plus markdown leakage), length adherence, format adherence, optional review-tier judge
   (`set_judge`). PROMOTE only if NLL improves by `gate_min_nll_gain` AND no metric regresses beyond its tolerance;
   fewer than `min_eval_examples` held-out items always rejects. Metrics are stored in `state.json` and `run.json`.
5. **Recursion and rollback** (`manager.py`): each run resumes from the promoted adapter; `state.json` keeps
   `promoted` and `previous` (rollback = `tune_run(trigger="rollback")`); old non-promoted adapters are pruned
   (`keep_runs`). `new_data` triggers only when >= `new_data_threshold` unseen user texts exist; `auto_run` lets
   `tune_status` start a due run. **Guards**: never on battery, never while a heavy (>= `heavy_model_gb`, default 4 GB) Ollama model is resident, unless
   `allow_on_battery` / `allow_with_heavy_model`. One run at a time.
6. **Tools**: `tune_status`, `tune_run(trigger, wait)` (deviation from ADR-0005's tool budget: two compact tools).
7. **Ollama export** (`export.py`, optional, needs `tune.ollama_base`): writes `Modelfile` with `FROM` + `ADAPTER`.
   Caveat: Ollama needs a PEFT-format adapter on the same architecture; MLX adapters may need conversion.

## Consequences
Personal data and adapters live under `<data_dir>/tuning` only. With very little data the gate will usually reject;
that is the intended safe behavior. No new core dependency (httpx already core; mlx-lm is an extra).

## Verify / revert
Verify: `uv run ruff check . && uv run pytest -q tests/test_tuning.py` (fake trainer, offline).
Revert: `git revert` the squash commit; delete `<data_dir>/tuning`.

"""Eval gate: candidate vs incumbent on held-out user texts; PROMOTE only if criteria hold (ADR-0017)."""
from __future__ import annotations

import math
import re
from collections.abc import Callable
from pathlib import Path
from typing import Any

from ..config import TuneCfg
from ..control.builtin import CONTRACTION_RX, DEFAULT_ENTHUSIASM, DEFAULT_GAP_PHRASES, phrase_regex
from ..writing import checks
from .trainer import Trainer

_GAP, _ENTH = phrase_regex(DEFAULT_GAP_PHRASES), phrase_regex(DEFAULT_ENTHUSIASM)
_MD = re.compile(r"(?m)^\s*(?:[#>*\-]\s|\d+\.\s)|\*\*|`")

def degenerate_count(outs: list[str], cfg: TuneCfg | None = None) -> int:
    """Outputs that loop, repeat sentences, or emit non-text runs (same check as the writing lint)."""
    return sum(1 for o in outs if checks.degenerate(o))


def leakage_count(outs: list[str], rows: list[dict]) -> int:
    """Outputs containing addressees/orgs/dates that belong to OTHER eval examples' letters but not to their own prompt."""
    n = 0
    texts = [r["messages"][2]["content"] for r in rows]
    for i, o in enumerate(outs):
        own = (rows[i]["messages"][0]["content"] + " " + rows[i]["messages"][1]["content"]).lower()
        low = o.lower()
        for j, t in enumerate(texts):
            if j != i and any(e.lower() in low and e.lower() not in own for e in checks.exemplar_entities(t)):
                n += 1
                break
    return n


Judge = Callable[[str, str, str], float | None]   # (prompt, base_out, cand_out) -> P(candidate better) or None
_judge: Judge | None = None


def set_judge(j: Judge | None) -> None:
    """Wire an optional review-tier judge (router task_class 'review'). Never required."""
    global _judge
    _judge = j


def lint_violations(text: str) -> int:
    """Style-lint hits using the writing pipeline's lexicons (control/builtin prose_lint) plus markdown leakage."""
    return (len(CONTRACTION_RX.findall(text)) + len(_GAP.findall(text)) + len(_ENTH.findall(text))
            + len(_MD.findall(text)))


def text_metrics(outs: list[str], refs: list[str]) -> dict[str, float]:
    n = max(1, len(outs))
    wc = sum(max(1, len(o.split())) for o in outs)
    lint = sum(lint_violations(o) for o in outs)
    len_err = sum(abs(len(o.split()) / max(1, len(r.split())) - 1) for o, r in zip(outs, refs, strict=False)) / n
    fmt_bad = sum(1 for o, r in zip(outs, refs, strict=False) if bool(_MD.search(o)) and not _MD.search(r)) / n
    return {"lint_per_100w": 100 * lint / wc, "len_err": len_err, "format_err": fmt_bad}


def score(trainer: Trainer, base: str, adapter: Path | None, rows: list[dict], cfg: TuneCfg) -> dict[str, float]:
    prompts = [r["messages"][:2] for r in rows]
    refs = [r["messages"][2]["content"] for r in rows]
    nll = trainer.nll(base, adapter, list(zip(prompts, refs, strict=True)))
    outs = trainer.generate(base, adapter, prompts, cfg.max_gen_tokens)
    m = text_metrics(outs, refs)
    m["nll"], m["ppl"] = nll, math.exp(min(nll, 50))
    m["_outs"] = outs  # type: ignore[assignment]
    return m


def evaluate(trainer: Trainer, cfg: TuneCfg, eval_rows: list[dict], candidate: Path,
             incumbent: Path | None) -> dict[str, Any]:
    """Compare candidate against incumbent (promoted adapter, or the bare base when none)."""
    if len(eval_rows) < cfg.min_eval_examples:
        return {"decision": "reject", "reasons": [
            f"only {len(eval_rows)} held-out examples (< min_eval_examples={cfg.min_eval_examples}); cannot gate"], "n_eval": len(eval_rows)}
    base = score(trainer, cfg.base_model, incumbent, eval_rows, cfg)
    cand = score(trainer, cfg.base_model, candidate, eval_rows, cfg)
    b_outs, c_outs = base.pop("_outs"), cand.pop("_outs")  # type: ignore[misc]
    reasons: list[str] = []
    gain = (base["nll"] - cand["nll"]) / max(1e-9, base["nll"])
    if gain <= cfg.gate_min_nll_gain:
        reasons.append(f"held-out loss gain {gain:+.1%} <= required {cfg.gate_min_nll_gain:.1%}")
    if cand["lint_per_100w"] > base["lint_per_100w"] + cfg.gate_max_lint_regress:
        reasons.append(f"lint violations rose ({base['lint_per_100w']:.2f} -> {cand['lint_per_100w']:.2f} /100w)")
    if cand["len_err"] > base["len_err"] + cfg.gate_max_length_regress:
        reasons.append(f"length adherence worse ({base['len_err']:.2f} -> {cand['len_err']:.2f})")
    if cand["format_err"] > base["format_err"]:
        reasons.append("format adherence worse")
    # Hard gates, independent of loss: degenerate output or exemplar leakage always REJECTs (ADR-0023).
    if n := degenerate_count(c_outs):
        reasons.append(f"degenerate output in {n} held-out generation(s) (loops/repeats/markdown runs)")
    if n := leakage_count(c_outs, eval_rows):
        reasons.append(f"exemplar leakage in {n} generation(s) (foreign addressee/org/date)")
    judge_p = None
    if cfg.use_judge and _judge:
        ps = [p for p in (_judge(r["messages"][1]["content"], b, c) for r, b, c in
                          zip(eval_rows, b_outs, c_outs, strict=True)) if p is not None]
        if ps:
            judge_p = sum(ps) / len(ps)
            if judge_p < 0.5:
                reasons.append(f"judge prefers incumbent ({judge_p:.2f})")
    return {"decision": "reject" if reasons else "promote", "reasons": reasons, "n_eval": len(eval_rows),
            "incumbent": "adapter" if incumbent else "base", "baseline": base, "candidate": cand,
            "nll_gain": gain, "judge_p": judge_p}

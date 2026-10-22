"""Comparative quality gate (ADR-0023): judge a draft against the user's own letters, not only against rules.

Deterministic metrics are computed for the draft and for the user's corpus (mean/sd across their letters):
evidence density, company-problem connection, structure fidelity, specificity, concision. A model rubric
(review tier; default a client task) scores argument / voice / precision / specificity 1-5 with the user's own
letters as the bar (style only). `pass` needs rule-clean text AND metrics >= baseline - tolerance AND rubric >= thresholds.
"""
from __future__ import annotations

import json
import re
import statistics
from pathlib import Path
from typing import Any

from ..config import WritingCfg
from ..profile.resume import norm

_SENT = re.compile(r"(?<=[.!?])\s+")
_NUM = re.compile(r"\$?\d[\d,.]*\s?(?:%|x\b|k\b|m\b)?")
_NAMED = re.compile(r"\b(?:[A-Z][a-z]+[A-Z][A-Za-z0-9]*|[A-Z]{2,}[0-9]*|[A-Z][a-z]{2,})\b")
_CAMEL = re.compile(r"\b(?:[A-Za-z]+[A-Z][A-Za-z0-9]*|[A-Z]{2,}[0-9]*)\b")
_GENERIC = re.compile(
    r"team player|fast learner|hard[- ]working|strong communication|wide range|various|many different|results[- ]driven|"
    r"detail[- ]oriented|fast[- ]paced|synerg|leverage|self[- ]motivated|go-getter|dynamic|passion|skill set|"
    r"unique opportunity|make a difference|think outside|best practices|value add", re.IGNORECASE)
_CONNECT = re.compile(r"\b(because|so that|which|therefore|this (?:is|maps|applies)|the same|addresses|applies to|"
                      r"relevant|directly|your (?:problem|goal|platform|team|customers|fleet))\b", re.IGNORECASE)
_STOP = {"which", "their", "there", "these", "those", "would", "about", "other", "while", "being", "build", "using"}
# metric name -> higher_is_better
METRICS = {"evidence_density": True, "problem_connection": True, "structure_fidelity": True,
           "specificity": True, "concision": False}


def _words(t: str) -> list[str]:
    return re.findall(r"[A-Za-z0-9'%$.+#-]+", t)


def _sents(t: str) -> list[str]:
    return [s for s in _SENT.split(re.sub(r"\n+", " ", t).strip()) if s]


def _evidence(sent: str, grounding: str) -> int:
    items = {m.group(0).strip().lower() for m in _NUM.finditer(sent) if len(m.group(0).strip()) > 1}
    items |= {m.group(0).lower() for m in _CAMEL.finditer(sent) if m.start() > 0}
    items |= {m.group(0).lower() for m in _NAMED.finditer(sent) if m.start() > 0}
    if grounding:  # resume-first: only facts the user can stand behind count as evidence
        items = {i for i in items if i in grounding or re.sub(r"[^a-z0-9]", "", i) in grounding}
    return len(items)


def _paras(t: str) -> list[str]:
    return [p for p in re.split(r"\n\s*\n", t.strip()) if p.strip()]


def metrics(text: str, *, grounding: str = "", posting: str = "", corpus: list[str] | None = None) -> dict[str, float]:
    sents = _sents(text)
    n_words = max(1, len(_words(text)))
    g = norm(grounding) + " " + grounding.lower()
    ev = [_evidence(s, g if grounding else "") for s in sents]
    spec_sents = sum(1 for s, e in zip(sents, ev, strict=True) if e and not _GENERIC.search(s))
    claim_sents = sum(1 for e in ev if e)
    problem = {w for w in norm(posting).split() if len(w) >= 5 and w not in _STOP}
    arg = " ".join(s for s in sents if _CONNECT.search(s) or any(w in norm(s).split() for w in list(problem)[:60]))
    prob = (len(problem & set(norm(arg).split())) / len(problem)) if problem else 1.0
    out = {
        "evidence_density": 100 * sum(ev) / n_words,
        "problem_connection": prob,
        "specificity": spec_sents / max(1, len(sents)),
        "concision": n_words / max(1, claim_sents),
    }
    out["structure_fidelity"] = structure_fidelity(text, corpus or [])
    return out


def structure_fidelity(text: str, corpus: list[str]) -> float:
    """Share of 4 checks passed: paragraph count, opening template, closing template, sentence-length band."""
    if not corpus:
        return 1.0
    paras, sents = _paras(text), _sents(text)
    cp = [len(_paras(c)) for c in corpus]
    ok = [min(cp) - 1 <= len(paras) <= max(cp) + 1]
    first3 = lambda s: " ".join(_words(s.lower())[:3])
    ok.append(any(first3(_sents(c)[0]) == first3(sents[0]) for c in corpus if _sents(c)) if sents else False)
    last3 = lambda s: " ".join(_words(s.lower())[:2])
    ok.append(any(last3(_sents(c)[-1]) == last3(sents[-1]) for c in corpus if _sents(c)) if sents else False)
    lens = [len(_words(s)) for c in corpus for s in _sents(c)]
    if lens and sents:
        mu, sd = statistics.mean(lens), statistics.pstdev(lens)
        mean = statistics.mean(len(_words(s)) for s in sents)
        ok.append(abs(mean - mu) <= max(sd, 0.25 * mu))
    else:
        ok.append(True)
    return sum(ok) / len(ok)


def baseline(corpus: list[str], *, grounding: str = "") -> dict[str, dict[str, float]]:
    rows = [metrics(c, grounding=grounding, corpus=corpus) for c in corpus]
    out: dict[str, dict[str, float]] = {}
    for k in METRICS:
        vals = [r[k] for r in rows] or [0.0]
        out[k] = {"mean": statistics.mean(vals), "sd": statistics.pstdev(vals) if len(vals) > 1 else 0.0}
    return out


def scorecard(text: str, cfg: WritingCfg, *, corpus: list[str], grounding: str = "", posting: str = "") -> dict[str, Any]:
    """Compact scorecard: per metric draft value, corpus mean, floor and pass flag, plus failing feedback."""
    d = metrics(text, grounding=grounding, posting=posting, corpus=corpus)
    base = baseline(corpus, grounding=grounding) if corpus else {}
    rows: dict[str, Any] = {}
    fails: list[str] = []
    for k, higher in METRICS.items():
        if k == "problem_connection":
            floor = cfg.quality_min_problem_overlap if posting else 0.0
            ok = d[k] >= floor
            rows[k] = {"v": round(d[k], 3), "floor": floor, "ok": ok}
        elif corpus:
            b = base[k]
            slack = cfg.quality_tolerance * max(b["sd"], 0.2 * abs(b["mean"]))
            if k == "structure_fidelity":
                slack = max(slack, 0.0) if b["sd"] else 0.25
            bound = b["mean"] - slack if higher else b["mean"] + slack
            ok = d[k] >= bound if higher else d[k] <= bound
            rows[k] = {"v": round(d[k], 3), "base": round(b["mean"], 3), "bound": round(bound, 3), "ok": ok}
        else:
            continue
        if not rows[k]["ok"]:
            fails.append(_advice(k, rows[k], higher))
    return {"metrics": rows, "fails": fails, "n_corpus": len(corpus), "ok": not fails}


def _advice(k: str, r: dict[str, Any], higher: bool) -> str:
    tips = {
        "evidence_density": "add concrete resume-backed facts, metrics and named systems; cut abstract sentences",
        "problem_connection": "tie each paragraph's evidence to the company's stated problem or duties",
        "structure_fidelity": "match the user's letters: paragraph count, opening and closing templates, sentence length",
        "specificity": "replace generic phrases with specific nouns, numbers and named work",
        "concision": "fewer words per claim; remove filler",
    }
    cmp = "below" if higher else "above"
    ref = r.get("bound", r.get("floor"))
    return f"{k} {r['v']} is {cmp} the bar {ref}: {tips[k]}"


def rubric_prompt(draft: str, refs: list[str], company: str, title: str, facts: list[str], cfg: WritingCfg) -> str:
    bar = "\n---\n".join(r[:1800] for r in refs[:2]) or "(no reference letters available)"
    need = ", ".join(f"{k}>={v}" for k, v in cfg.quality_min_scores.items())
    return (f"You are the quality gate for a cover letter to {company!r} for {title!r}.\n"
            "REFERENCE LETTERS WRITTEN BY THE USER (the quality bar; STYLE ONLY: never take their facts, names, "
            f"addressees or dates):\n{bar}\n\nALLOWED FACTS (resume first):\n" + "\n".join(f"- {f}" for f in facts[:20]) +
            f"\n\nDRAFT:\n{draft}\n\nScore 1-5: argument (each paragraph ties the evidence to the company's problem as the "
            "reference letters do), voice (tone and diction match the reference letters), precision (every claim is in the "
            "allowed facts), specificity (not generic). Answer: is this at least as strong as the reference letters, and why "
            "or why not? Give sentence-level edits. Return JSON "
            '{"scores":{"argument":n,"voice":n,"precision":n,"specificity":n},"at_least_as_strong":bool,"why":str,'
            f'"edits":[str],"corrected":str}}. Pass thresholds: {need}.')


def strict_verdict(v: object) -> bool:
    """A reviewer verdict passes only as JSON true or the exact string "true"; anything else, including a missing
    value, "false", "yes" or 1, fails closed (ADR-0044)."""
    return v is True or v == "true"


def parse_rubric(raw: str, cfg: WritingCfg) -> tuple[bool, dict[str, int], list[str], str]:
    m = re.search(r"\{.*\}", raw, re.DOTALL)
    if not m:
        return False, {}, ["rubric reviewer returned no JSON"], ""
    try:
        d = json.loads(m.group(0))
        scores = {k: int(v) for k, v in (d.get("scores") or {}).items()}
    except (ValueError, TypeError):
        return False, {}, ["rubric reviewer returned invalid JSON"], ""
    low = [f"{k} {scores.get(k, 0)} < {v}" for k, v in cfg.quality_min_scores.items() if scores.get(k, 0) < v]
    edits = [str(e) for e in d.get("edits", [])]
    ok = not low and strict_verdict(d.get("at_least_as_strong"))
    why = [f"rubric: {x}" for x in low] + ([] if ok or low else [f"not as strong as reference: {d.get('why', '')}"])
    return ok, scores, why + edits[:4], str(d.get("corrected") or "")


def client_rubric(refs: list[str], cfg: WritingCfg) -> str:
    need = ", ".join(f"{k}>={v}" for k, v in cfg.quality_min_scores.items())
    return (f" QUALITY BAR: compare with the user's own letters ({len(refs)} on file; style only, never their facts). "
            f"Score 1-5 ({need}) and answer 'is this at least as strong as the reference letters? why/why not?' "
            "with sentence-level edits.")


def read_corpus(paths: list[str], limit: int = 8, chars: int = 6000) -> list[str]:
    out: list[str] = []
    for raw in paths:
        p = Path(raw).expanduser()
        files = sorted(p.glob("*")) if p.is_dir() else [p]
        for f in files:
            if f.is_file() and f.suffix.lower() in {".txt", ".md"}:
                try:
                    t = f.read_text(encoding="utf-8")[:chars].strip()
                except OSError:
                    continue
                if t:
                    out.append(t)
    return out[:limit]

"""Two-stage prose pipeline for cover letters and long free-text answers (ADR-0013).

writer (task_class="narrative") -> deterministic lint -> reviewer (task_class="review" or the calling agent).
Lint runs first so cheap regex checks reject bad drafts before any reviewer tokens are spent.
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path
from typing import Any

from ..config import WritingCfg
from ..profile.resume import Fact, today_local
from ..writing import checks, quality
from .ports import RouterLike

_CONTRACTION = re.compile(
    r"\b(?:\w+n't|\w+'(?:re|ve|ll|d|m)|(?:it|that|there|here|let|what|who|he|she)'s)\b", re.IGNORECASE)
_TAGS = re.compile(r"<[^>]+>")
_SENT = re.compile(r"(?<=[.!?])\s+")
_SUMMARY_HINTS = re.compile(
    r"\b(mission|problem|we are|we're|we build|our (?:product|platform|team)|you will|responsibilit|"
    r"build|design|develop|own|lead|maintain)\b", re.IGNORECASE)


@dataclass
class Violation:
    kind: str       # banned_phrase | contraction | gap | length | structure | company | copied | model |
                    # placeholder | addressee | temporal | degenerate | closing | leakage | quality
    detail: str
    sentence: str = ""

    def line(self) -> str:
        return f"[{self.kind}] {self.detail}" + (f' in: "{self.sentence[:120]}"' if self.sentence else "")


@dataclass
class WritingResult:
    status: str                      # pass | needs_review | needs_human
    text: str
    violations: list[Violation] = field(default_factory=list)
    rounds: int = 0
    rubric: str = ""                 # for client review: what the calling agent must check
    scorecard: dict[str, Any] = field(default_factory=dict)   # compact quality scorecard (ADR-0023)


def summarize_posting(description: str | None, limit: int = 900) -> str:
    """Compact, deterministic summary: sentences that state the company's problem or the role's duties."""
    if not description:
        return ""
    text = re.sub(r"\s+", " ", _TAGS.sub(" ", description)).strip()
    picked: list[str] = []
    total = 0
    for sent in _SENT.split(text):
        if 25 <= len(sent) <= 300 and _SUMMARY_HINTS.search(sent):
            if total + len(sent) > limit:
                break
            picked.append(sent)
            total += len(sent) + 1
    return " ".join(picked) or text[:limit]


def load_exemplar(path: str | None, limit: int = 3500) -> str:
    if not path:
        return ""
    try:
        return Path(path).expanduser().read_text(encoding="utf-8")[:limit]
    except OSError:
        return ""


def _sentences(text: str) -> list[str]:
    return [s for s in _SENT.split(text.strip()) if s]


def _shingles(text: str, n: int = 8) -> set[tuple[str, ...]]:
    w = re.findall(r"[a-z0-9']+", text.lower())
    return {tuple(w[i:i + n]) for i in range(max(0, len(w) - n + 1))}


def lint(text: str, cfg: WritingCfg, *, company: str = "", max_len: int | None = None,
         exemplar: str = "", resume: list[Fact] | None = None, today: date | None = None) -> list[Violation]:
    out: list[Violation] = []
    long_form = len(text) > 600
    hits = checks.placeholders(text) + checks.degenerate(
        text, max_nonalpha=cfg.degenerate_max_nonalpha, repeat=cfg.degenerate_repeat)
    if long_form:
        hits += checks.addressee(text, company) + checks.closing(text, cfg.required_closing)
    hits += checks.temporal(text, resume or [], today or today_local())
    if exemplar:
        hits += checks.leakage(text, exemplar, company)
    out += [Violation(k, d, s) for k, d, s in hits]
    for sent in _sentences(text):
        low = sent.lower()
        for ph in cfg.banned_phrases:
            if re.search(rf"(?<!\w){re.escape(ph.lower())}", low):
                out.append(Violation("banned_phrase", f'"{ph}"', sent))
        m = _CONTRACTION.search(sent)
        if m:
            out.append(Violation("contraction", f'"{m.group(0)}"', sent))
        for pat in cfg.gap_patterns:
            if re.search(pat, sent, re.IGNORECASE):
                out.append(Violation("gap", f"gap or deficiency statement ({pat})", sent))
    paras = [p for p in re.split(r"\n\s*\n", text.strip()) if p.strip()]
    if long_form and not (cfg.paragraphs_min <= len(paras) <= cfg.paragraphs_max):
        out.append(Violation("structure", f"{len(paras)} paragraphs; want {cfg.paragraphs_min}-{cfg.paragraphs_max}"))
    if max_len and len(text) > max_len:
        out.append(Violation("length", f"{len(text)} chars exceeds {max_len}"))
    if company and long_form and company.lower() not in text.lower():
        out.append(Violation("company", f"company name '{company}' never appears"))
    if exemplar and _shingles(text) & _shingles(exemplar):
        out.append(Violation("copied", "8+ consecutive words copied from the style exemplar"))
    return out


def _writer_prompt(*, kind: str, label: str, company: str, title: str, summary: str, facts: list[str],
                   cfg: WritingCfg, exemplar: str, max_len: int | None, timeline: list[str] | None = None) -> str:
    rules = "\n".join(f"- {r}" for r in cfg.rules)
    length = (f"At most {max_len} characters." if max_len else
              f"{cfg.paragraphs_min}-{cfg.paragraphs_max} paragraphs." if kind == "cover_letter" else
              "One to three short paragraphs.")
    ex = (f"\nSTYLE EXEMPLAR (style only — do not copy facts, names, addressees, dates; address {company!r} only):"
          f"\n{exemplar}\n" if exemplar else "")
    tl = ("\nTIMELINE (from the resume, which is authoritative; follow these tenses):\n" +
          "\n".join(f"- {t}" for t in (timeline or [])) + "\n" if timeline else "")
    return (f"Write the answer to: {label!r} for the role {title!r} at {company!r}.\n"
            f"RULES:\n{rules}\n{length}\n\n"
            f"COMPANY/ROLE CONTEXT (their stated problem and the duties):\n{summary or '(none provided)'}\n\n"
            f"FACTS (resume first; the only claims you may make):\n" + "\n".join(f"- {f}" for f in facts) +
            f"{tl}{ex}\nNo placeholders such as [Your Name]; plain text only, no markdown. "
            "Return only the final text, no preamble.")


def _revise_prompt(draft: str, violations: list[Violation], max_len: int | None) -> str:
    v = "\n".join(f"- {x.line()}" for x in violations)
    cap = f" Keep it at most {max_len} characters." if max_len else ""
    return (f"Revise the draft to fix every violation. Remove offending sentences rather than rephrasing a banned "
            f"idea.{cap} Return only the corrected text.\n\nVIOLATIONS:\n{v}\n\nDRAFT:\n{draft}")


def review_rubric(cfg: WritingCfg, *, company: str, title: str, facts: list[str]) -> str:
    return ("Review sentence by sentence. Fail on: claims not supported by the facts; any gap, deficiency or "
            "'would need to learn' statement; enthusiasm, personal-feeling or flattery phrases "
            f"(lexicon: {', '.join(cfg.banned_phrases[:12])}); contractions; wrong company/role names "
            f"(expected {company!r} / {title!r}); length; style mismatch. Rules: " + " ".join(cfg.rules) +
            " Facts: " + " | ".join(facts))


def _reviewer_prompt(draft: str, rubric: str) -> str:
    return (f"{rubric}\n\nDRAFT:\n{draft}\n\nReturn JSON "
            '{"pass": bool, "violations": [str], "corrected": str} where corrected is a fixed draft '
            "(identical to the draft when pass is true).")


def _parse_review(raw: str) -> tuple[bool, list[str], str]:
    m = re.search(r"\{.*\}", raw, re.DOTALL)
    if not m:
        return False, ["reviewer returned no JSON"], ""
    try:
        d = json.loads(m.group(0))
        return bool(d.get("pass")), [str(x) for x in d.get("violations", [])], str(d.get("corrected") or "")
    except (ValueError, TypeError):
        return False, ["reviewer returned invalid JSON"], ""


async def write_text(router: RouterLike, cfg: WritingCfg, *, kind: str, label: str, company: str, title: str,
                     summary: str, facts: list[str], max_len: int | None = None,
                     exemplar: str = "", resume: list[Fact] | None = None, corpus: list[str] | None = None,
                     today: date | None = None) -> WritingResult:
    """kind: 'cover_letter' | 'free_text'. Returns pass / needs_review (client reviews) / needs_human.

    `facts` must come from the precedence-resolved fact set (profile/facts.py); `resume` drives temporal checks
    and tense guidance; `corpus` (the user's own letters) is the quality baseline (writing/quality.py)."""
    today = today or today_local()
    from ..profile.resume import temporal_phrasing
    timeline = temporal_phrasing(resume, today) if resume else []
    refs = corpus or []
    gate = cfg.quality_gate and kind == "cover_letter" and bool(refs)
    rubric = review_rubric(cfg, company=company, title=title, facts=facts) + (
        quality.client_rubric(refs, cfg) if gate else "")
    draft = (await router.complete(
        _writer_prompt(kind=kind, label=label, company=company, title=title, summary=summary, facts=facts,
                       cfg=cfg, exemplar=exemplar, max_len=max_len, timeline=timeline),
        task_class="narrative", max_tokens=1200)).strip()
    violations: list[Violation] = []
    card: dict[str, Any] = {}
    grounding = " ".join(facts)
    for rnd in range(1, max(1, cfg.max_rounds) + 1):
        violations = lint(draft, cfg, company=company, max_len=max_len, exemplar=exemplar, resume=resume, today=today)
        if gate and not violations:
            card = quality.scorecard(draft, cfg, corpus=refs, grounding=grounding, posting=summary)
            violations = [Violation("quality", f) for f in card["fails"]]
        if not violations and cfg.reviewer == "client":
            return WritingResult("needs_review", draft, [], rnd, rubric, card)
        if not violations:  # router reviewer
            raw = await router.complete(
                quality.rubric_prompt(draft, refs, company, title, facts, cfg) if gate else _reviewer_prompt(draft, rubric),
                task_class="review", max_tokens=1500)
            if gate:
                ok, scores, issues, corrected = quality.parse_rubric(raw, cfg)
                card["rubric"] = scores
            else:
                ok, issues, corrected = _parse_review(raw)
            if ok:
                return WritingResult("pass", draft, [], rnd, "", card)
            violations = [Violation("model", i) for i in issues] or [Violation("model", "reviewer failed draft")]
            if corrected and corrected != draft:
                draft = corrected
                continue
        if rnd < cfg.max_rounds:
            draft = (await router.complete(_revise_prompt(draft, violations, max_len),
                                           task_class="narrative", max_tokens=1200)).strip()
    return WritingResult("needs_human", draft, violations, cfg.max_rounds, rubric, card)

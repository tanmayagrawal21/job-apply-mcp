"""Transparent, deterministic relevance ranking for search results (ADR-0022). Stdlib only.

Hard constraints (policy gates) run before this stage. Each signal is in [-1, 1] (or [0, 1]), multiplied by a
config weight, and carries a short reason; the top `max_why` reasons by contribution are returned.
An optional embedding rerank (local Ollama) can be blended in; it is off by default and fails soft.
"""
from __future__ import annotations

import math
import re
from collections import Counter
from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime

from ..config import PolicyCfg, SearchCfg
from ..core.models import Posting
from ..policy.gates import effective_floor, parse_salary

_TOK = re.compile(r"[a-z0-9][a-z0-9+#.]*[a-z0-9+#]|[a-z0-9]")
_STOP = {"the", "a", "an", "and", "or", "of", "for", "to", "in", "at", "with", "on", "is", "are", "we", "you"}
_LEVEL_WORDS = {
    "entry": {"entry", "junior", "jr", "associate", "intern", "internship", "grad", "graduate", "i", "new"},
    "mid": {"ii", "iii", "mid"},
    "senior": {"senior", "sr", "staff", "principal", "lead", "director", "head", "manager", "vp", "iv"},
}


def tokens(text: str) -> list[str]:
    return [t for t in _TOK.findall((text or "").lower()) if t not in _STOP]


def bm25_scores(query: Sequence[str], docs: Sequence[Sequence[str]], k1: float = 1.4, b: float = 0.75) -> list[float]:
    """Okapi BM25 of one query against a small corpus (idf is corpus-local, floored at a small positive value)."""
    n = len(docs)
    if not n or not query:
        return [0.0] * n
    avg = (sum(len(d) for d in docs) / n) or 1.0
    df = Counter(t for d in docs for t in set(d))
    out = []
    for d in docs:
        tf, score = Counter(d), 0.0
        for t in set(query):
            if not tf[t]:
                continue
            idf = max(math.log(1 + (n - df[t] + 0.5) / (df[t] + 0.5)), 0.1)
            score += idf * tf[t] * (k1 + 1) / (tf[t] + k1 * (1 - b + b * len(d) / avg))
        out.append(score)
    return out


@dataclass
class Ranked:
    posting: Posting
    score: float = 0.0
    why: list[str] = field(default_factory=list)
    q_hit: bool = True


def _skills_hit(skills: Iterable[str], text: str, toks: set[str]) -> list[str]:
    hit = []
    for s in skills:
        st = tokens(s)
        if not st:
            continue
        if len(st) == 1 and st[0] in toks or len(st) > 1 and " ".join(st) in text:
            hit.append(s)
    return hit


def _level(title: str) -> str | None:
    tk = set(tokens(title))
    for lv in ("senior", "entry", "mid"):  # senior words win ("senior associate" is senior)
        if tk & _LEVEL_WORDS[lv]:
            return lv
    return None


def _age_days(posted: str | None, now: datetime) -> float | None:
    try:
        d = datetime.fromisoformat((posted or "")[:10]).replace(tzinfo=UTC)
    except ValueError:
        return None
    return max(0.0, (now - d).total_seconds() / 86400)


def rank_postings(postings: Sequence[Posting], query: str, skills: Sequence[str], cfg: SearchCfg,
                  policy: PolicyCfg | None = None, now: datetime | None = None) -> list[Ranked]:
    """Score and sort. Postings without a description are scored on title/location/comp/recency only."""
    now = now or datetime.now(UTC)
    w = cfg.weights
    q = tokens(query)
    docs = [tokens(p.title) * 3 + tokens(p.description or "") for p in postings]
    bm = bm25_scores(q, docs)
    top_bm = max(bm, default=0.0) or 1.0
    out: list[Ranked] = []
    for p, doc, raw in zip(postings, docs, bm, strict=True):
        parts: list[tuple[float, str]] = []
        text, tset = " ".join(doc), set(doc)
        title_t = set(tokens(p.title))
        q_hit = not q or raw > 0
        if q:
            sig = raw / top_bm
            in_title = [t for t in dict.fromkeys(q) if t in title_t]
            in_desc = [t for t in dict.fromkeys(q) if t in tset and t not in title_t]
            reason = "query: " + ", ".join([f"{t} (title)" for t in in_title[:2]] + [f"{t} (desc)" for t in in_desc[:2]]) \
                if (in_title or in_desc) else "query: no term match"
            parts.append((w["query"] * sig, reason))
        hit = _skills_hit(skills, text, tset) if skills else []
        if skills:
            frac = len(hit) / len(skills)
            if hit:
                parts.append((w["skills"] * min(1.0, frac * 2), "skills: " + ", ".join(hit[:3])))
        if cfg.seniority:
            lv = _level(p.title)
            if lv == cfg.seniority:
                parts.append((w["seniority"], f"level fit: {lv}"))
            elif lv:
                parts.append((-w["seniority"], f"level mismatch: {lv}"))
        if cfg.location_order:
            loc = (p.location or "").lower()
            n = len(cfg.location_order)
            idx = next((i for i, s in enumerate(cfg.location_order) if s.lower() in loc), None)
            if idx is not None:
                parts.append((w["location"] * (1 - idx / n), f"location pref #{idx + 1}: {cfg.location_order[idx]}"))
            else:
                parts.append((-0.5 * w["location"], "location not preferred"))
        if policy and policy.salary_floor and p.salary and (rng := parse_salary(p.salary, policy.hours_per_year)):
            floor = effective_floor(policy, p.location) or policy.salary_floor
            if rng[1] >= floor:
                parts.append((w["comp"], "pay meets floor"))
            else:
                parts.append((-w["comp"], "pay below floor"))
        age = _age_days(p.posted_at, now)
        if age is not None:
            parts.append((w["recency"] * 0.5 ** (age / max(cfg.recency_half_life_days, 0.1)),
                          f"posted {int(age)}d ago" if age >= 1 else "posted today"))
        max_total = sum(w[k] for k in ("query", "skills", "seniority", "location", "comp", "recency")
                        if w.get(k)) or 1.0
        score = 100 * max(0.0, sum(c for c, _ in parts)) / max_total
        why = [r for c, r in sorted(parts, key=lambda x: -abs(x[0])) if abs(c) > 1e-9][: cfg.max_why]
        out.append(Ranked(p, round(score, 1), why, q_hit))
    out.sort(key=lambda r: (r.score, r.posting.posted_at or "", r.posting.ref), reverse=True)
    return out


def cosine(a: Sequence[float], b: Sequence[float]) -> float:
    na, nb = math.sqrt(sum(x * x for x in a)), math.sqrt(sum(x * x for x in b))
    return sum(x * y for x, y in zip(a, b, strict=False)) / (na * nb) if na and nb else 0.0


async def embed_rerank(ranked: list[Ranked], query: str, profile_text: str, cfg: SearchCfg) -> list[Ranked]:
    """Blend embedding similarity (query + profile skills vs title/description) into the score. Fails soft:
    on any error the input order is returned unchanged. Imported lazily so the default path needs no httpx call."""
    import httpx

    texts = [f"{query}\n{profile_text}"[:2000]] + [
        f"{r.posting.title}\n{(r.posting.description or '')[:1500]}" for r in ranked]
    try:
        async with httpx.AsyncClient(timeout=30) as c:
            resp = await c.post(f"{cfg.embed_url.rstrip('/')}/api/embed", json={"model": cfg.embed_model, "input": texts})
            resp.raise_for_status()
            vecs = resp.json()["embeddings"]
        if len(vecs) != len(texts):
            return ranked
    except (httpx.HTTPError, KeyError, ValueError):
        return ranked
    for r, v in zip(ranked, vecs[1:], strict=True):
        sim = max(0.0, cosine(vecs[0], v))
        r.score = round(r.score + cfg.embed_weight * sim * 100 / (sum(cfg.weights.values()) or 1.0), 1)
        r.why = ([f"semantic {sim:.2f}"] + r.why)[: cfg.max_why]
    ranked.sort(key=lambda r: (r.score, r.posting.posted_at or "", r.posting.ref), reverse=True)
    return ranked

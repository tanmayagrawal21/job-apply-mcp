"""Dataset builder: SFT pairs + preference pairs from user-authored text only (ADR-0017)."""
from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from ..config import TuneCfg
from .source import origin_of, trusted_items

SYSTEM = "You write in the user's own voice: plain, concrete, first person, no filler."


def words(t: str) -> int:
    return len(t.split())


def _norm(t: str) -> str:
    return re.sub(r"\W+", " ", t.lower()).strip()


def _shingles(t: str) -> set[str]:
    w = _norm(t).split()
    return {" ".join(w[i:i + 3]) for i in range(max(1, len(w) - 2))}


def reverse_unified_diff(edited: str, diff: str) -> str | None:
    """Recover the parent (LLM draft) from the edited text and a unified diff parent->edited.
    The LLM original is not reachable through DatasetSource by design; the diff makes it recoverable."""
    new_lines = edited.splitlines()
    out: list[str] = []
    pos = 0                       # index into new_lines
    hunks = re.split(r"(?m)^@@ -\d+(?:,\d+)? \+(\d+)(?:,(\d+))? @@.*$", diff)
    if len(hunks) < 4:
        return None
    for i in range(1, len(hunks), 3):
        start = int(hunks[i]) - 1
        body = hunks[i + 2].split("\n")
        if start < pos or start > len(new_lines):
            return None
        out += new_lines[pos:start]
        pos = start
        for ln in body:
            if not ln or ln.startswith("\\"):
                continue
            tag, txt = ln[0], ln[1:]
            if tag == " ":
                if pos >= len(new_lines) or new_lines[pos] != txt:
                    return None
                out.append(txt)
                pos += 1
            elif tag == "+":
                if pos >= len(new_lines) or new_lines[pos] != txt:
                    return None
                pos += 1
            elif tag == "-":
                out.append(txt)
    out += new_lines[pos:]
    return "\n".join(out)


@dataclass
class Dataset:
    run_dir: Path
    train: list[dict] = field(default_factory=list)
    eval: list[dict] = field(default_factory=list)
    prefs: list[dict] = field(default_factory=list)
    source_ids: list[str] = field(default_factory=list)
    dropped: dict[str, int] = field(default_factory=dict)

    def summary(self) -> dict[str, Any]:
        return {"train": len(self.train), "eval": len(self.eval), "prefs": len(self.prefs),
                "texts": len(self.source_ids), "dropped": self.dropped}


def _instruction(rec: Any, facts: str, cfg: TuneCfg, para: tuple[int, int] | None) -> str:
    ctx = (rec.context or "").strip()[: cfg.context_chars]
    head = f"Write the {rec.kind.replace('_', ' ')}"
    if para:
        head += f", paragraph {para[0]} of {para[1]}"
    parts = [head + "."]
    if ctx:
        parts.append(f"Context: {ctx}")
    if facts:
        parts.append(f"Facts:\n{facts}")
    return "\n".join(parts)


def _chat(instr: str, out: str) -> dict:
    return {"messages": [{"role": "system", "content": SYSTEM}, {"role": "user", "content": instr},
                         {"role": "assistant", "content": out}]}


def build_dataset(source: Any, cfg: TuneCfg, run_dir: Path, exclude_ids: set[str] | None = None) -> Dataset:
    """Pure function of (source, cfg): deterministic, writes JSONL into run_dir."""
    ds = Dataset(run_dir=run_dir)
    drop = ds.dropped
    bump = lambda k: drop.__setitem__(k, drop.get(k, 0) + 1)
    recs = trusted_items(source, None)
    facts_txt = " ".join(r.text.strip().replace("\n", " ") for r in recs if r.kind == cfg.facts_kind)
    facts = facts_txt[: cfg.facts_chars]
    texts = [r for r in recs if r.kind in cfg.kinds and r.kind != cfg.facts_kind]
    texts.sort(key=lambda r: r.id)

    seen: list[set[str]] = []
    units: list[tuple[str, dict, str]] = []     # (group id, example, source id)
    for r in texts:
        if exclude_ids and r.id in exclude_ids:
            bump("already_trained")
            continue
        chunks: list[tuple[str, tuple[int, int] | None]]
        if cfg.paragraph_split:
            paras = [p.strip() for p in re.split(r"\n\s*\n", r.text) if p.strip()]
            chunks = [(p, (i + 1, len(paras))) for i, p in enumerate(paras)]
        else:
            chunks = [(r.text.strip(), None)]
        for body, para in chunks:
            w = words(body)
            if w < cfg.min_words or w > cfg.max_words:
                bump("length")
                continue
            sh = _shingles(body)
            if any(len(sh & o) / max(1, len(sh | o)) > 0.9 for o in seen):
                bump("duplicate")
                continue
            seen.append(sh)
            units.append((r.id, _chat(_instruction(r, facts, cfg, para), body), r.id))
    ds.source_ids = sorted({u[2] for u in units})

    # deterministic split by hash of the example; always keep >=1 train example
    def bucket(u: tuple[str, dict, str]) -> float:
        h = hashlib.sha256(f"{cfg.seed}:{u[1]['messages'][2]['content']}".encode()).digest()
        return int.from_bytes(h[:4], "big") / 2**32

    ranked = sorted(units, key=bucket)
    n_eval = round(len(ranked) * cfg.eval_fraction) if len(ranked) >= 2 else 0
    if cfg.eval_fraction > 0 and len(ranked) >= 2:
        n_eval = max(1, min(n_eval, len(ranked) - 1))
    ds.eval = [u[1] for u in ranked[:n_eval]]
    ds.train = [u[1] for u in ranked[n_eval:]]

    # preference pairs: user_edited records whose LLM draft is recoverable from the stored diff
    for r in recs:
        if origin_of(r) != "user_edited" or r.kind not in cfg.kinds or not r.diff_from_parent:
            continue
        rejected = reverse_unified_diff(r.text, r.diff_from_parent)
        if not rejected or _norm(rejected) == _norm(r.text):
            bump("pref_unrecoverable")
            continue
        ds.prefs.append({"prompt": _instruction(r, facts, cfg, None), "chosen": r.text.strip(),
                         "rejected": rejected.strip()})

    run_dir.mkdir(parents=True, exist_ok=True)
    for name, rows in (("train", ds.train), ("valid", ds.eval), ("preferences", ds.prefs)):
        (run_dir / f"{name}.jsonl").write_text("".join(json.dumps(x, ensure_ascii=False) + "\n" for x in rows))
    (run_dir / "dataset.json").write_text(json.dumps({**ds.summary(), "source_ids": ds.source_ids}))
    return ds

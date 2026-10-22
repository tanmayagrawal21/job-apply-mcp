"""Tag exports so LLM-generated and user content can be told apart (configurable; ADR-0019).

Modes:
  none     no tagging
  sidecar  write `<file>.provenance.json` next to the exported file (default; never leaves the machine
           unless the user attaches it)
  metadata return key/value pairs for the caller to embed as hidden document metadata (docx/pdf properties)
"""
from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any

from .store import ProvenanceStore

MODES = ("none", "sidecar", "metadata")


def manifest(store: ProvenanceStore, ids: list[str]) -> dict[str, Any]:
    items = []
    for rid in ids:
        r = store.get(rid)
        if r is None:
            items.append({"id": rid, "error": "unknown"})
            continue
        items.append({**r.compact(), "llm": r.llm_generated, "sha256": hashlib.sha256(r.text.encode()).hexdigest()[:16]})
    return {"items": items, "llm_ids": [i["id"] for i in items if i.get("llm")]}


def tag_export(store: ProvenanceStore, ids: list[str], mode: str, target: str | None = None) -> dict[str, Any]:
    if mode not in MODES:
        raise ValueError(f"unknown export_tagging '{mode}'; known: {list(MODES)}")
    if mode == "none":
        return {"mode": mode}
    m = manifest(store, ids)
    if mode == "metadata":
        return {"mode": mode, "metadata": {
            "Provenance-Origins": ",".join(f"{i['id']}:{i.get('origin', '?')}" for i in m["items"]),
            "Provenance-LLM-Generated": ",".join(m["llm_ids"]) or "none"}}
    if not target:
        raise ValueError("sidecar mode needs the exported file path as `target`")
    side = Path(target).expanduser().with_name(Path(target).name + ".provenance.json")
    side.write_text(json.dumps(m, indent=1))
    return {"mode": mode, "sidecar": str(side), "llm_ids": m["llm_ids"]}

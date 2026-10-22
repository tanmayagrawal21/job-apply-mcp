"""Per-platform playbook memory: mechanics-only lessons that adapters and the browser driver consult.

Stored at <data_dir>/playbooks/<platform>.json (machine) with a `.md` rendering. Never holds personal
data: text is scrubbed of emails/phones/URLs/long digit runs and capped before it is stored.
"""
from __future__ import annotations

import json
import re
import time
from pathlib import Path

_SCRUBS = [
    (re.compile(r"[\w.+-]+@[\w-]+\.[\w.]+"), "<email>"),
    (re.compile(r"https?://\S+|www\.\S+"), "<url>"),
    (re.compile(r"(?:/Users|/home)/[^/\s'\"]+|[A-Za-z]:\\Users\\[^\\\s]+"), "<home>"),
    (re.compile(r"\+?\d[\d\s().-]{7,}\d"), "<num>"),
]


def redact(text: str) -> str:
    """Remove emails/URLs/home paths/phone-like numbers but keep layout (for multi-line PR bodies)."""
    for rx, rep in _SCRUBS:
        text = rx.sub(rep, text)
    return text


def scrub(text: str, limit: int = 240) -> str:
    for rx, rep in _SCRUBS:
        text = rx.sub(rep, text)
    return re.sub(r"\s+", " ", text).strip()[:limit]


def _safe_name(platform: str) -> str:
    return re.sub(r"[^a-z0-9_-]", "", platform.lower()) or "generic"


class PlaybookStore:
    def __init__(self, root: Path):
        self.root = Path(root)

    def path(self, platform: str) -> Path:
        return self.root / f"{_safe_name(platform)}.json"

    def load(self, platform: str) -> dict:
        p = self.path(platform)
        if p.is_file():
            try:
                return json.loads(p.read_text(encoding="utf-8"))
            except json.JSONDecodeError:
                pass
        return {"platform": _safe_name(platform), "lessons": []}

    def add_lesson(self, platform: str, key: str, text: str, *, tags: list[str] | None = None,
                   evidence: int = 1, source: str = "analyzer") -> bool:
        """Upsert by `key`. Returns True if the playbook changed."""
        pb = self.load(platform)
        text = scrub(text)
        for les in pb["lessons"]:
            if les["key"] == key:
                if les["text"] == text and les["evidence"] >= evidence:
                    return False
                les.update(text=text, evidence=max(evidence, les["evidence"]), updated=int(time.time()))
                break
        else:
            pb["lessons"].append({"key": scrub(key, 80), "text": text, "tags": tags or [], "evidence": evidence,
                                  "source": source, "updated": int(time.time())})
        self._save(pb)
        return True

    def lessons(self, platform: str, *, tag: str | None = None, max_chars: int = 1200) -> list[str]:
        """Compact, most-evidenced-first lesson texts for prompts; capped to `max_chars`."""
        items = sorted(self.load(platform)["lessons"], key=lambda x: -x["evidence"])
        out, used = [], 0
        for les in items:
            if tag and tag not in les.get("tags", []):
                continue
            if used + len(les["text"]) > max_chars:
                break
            out.append(les["text"])
            used += len(les["text"])
        return out

    def _save(self, pb: dict) -> None:
        self.root.mkdir(parents=True, exist_ok=True)
        p = self.path(pb["platform"])
        p.write_text(json.dumps(pb, indent=1, sort_keys=True), encoding="utf-8")
        md = [f"# {pb['platform']} playbook (mechanics only)", ""]
        md += [f"- {x['text']} (seen {x['evidence']}x)" for x in sorted(pb["lessons"], key=lambda x: -x["evidence"])]
        p.with_suffix(".md").write_text("\n".join(md) + "\n", encoding="utf-8")

"""SQLite-backed provenance store. Own file and own tables; independent of store/ (ADR-0019).

Records are append-only. Approving or editing LLM text creates a child record, so the original LLM output
and the human decision are both preserved and the lineage can be audited.
"""
from __future__ import annotations

import difflib
import hashlib
import sqlite3
import threading
import uuid
from datetime import UTC, datetime
from pathlib import Path

from .models import Origin, ProvenanceRecord

_SCHEMA = """
CREATE TABLE IF NOT EXISTS prov_text (
  id TEXT PRIMARY KEY, kind TEXT NOT NULL, text TEXT NOT NULL, origin TEXT NOT NULL,
  model TEXT, prompt_hash TEXT, parent_id TEXT REFERENCES prov_text(id), created_at TEXT NOT NULL,
  verified_by TEXT, diff_from_parent TEXT, context TEXT
);
CREATE INDEX IF NOT EXISTS prov_text_parent ON prov_text(parent_id);
CREATE INDEX IF NOT EXISTS prov_text_origin ON prov_text(origin, kind);
"""
_COLS = ["id", "kind", "text", "origin", "model", "prompt_hash", "parent_id", "created_at", "verified_by", "diff_from_parent", "context"]


def prompt_hash(prompt: str) -> str:
    return hashlib.sha256(prompt.encode()).hexdigest()[:16]


def _diff(old: str, new: str) -> str:
    return "".join(difflib.unified_diff(old.splitlines(True), new.splitlines(True), "parent", "child", n=1))


class ProvenanceError(ValueError):
    pass


class ProvenanceStore:
    def __init__(self, path: str | Path):
        self.path = str(path)
        if self.path != ":memory:":
            Path(self.path).expanduser().parent.mkdir(parents=True, exist_ok=True)
            self.path = str(Path(self.path).expanduser())
        self._lock = threading.Lock()
        self._db = sqlite3.connect(self.path, check_same_thread=False)
        self._db.row_factory = sqlite3.Row
        self._db.executescript(_SCHEMA)

    def close(self) -> None:
        self._db.close()

    def _row(self, r: sqlite3.Row | None) -> ProvenanceRecord | None:
        return ProvenanceRecord(**{c: r[c] for c in _COLS}) if r else None

    def get(self, rid: str) -> ProvenanceRecord | None:
        with self._lock:
            return self._row(self._db.execute("SELECT * FROM prov_text WHERE id=?", (rid,)).fetchone())

    def _insert(self, rec: ProvenanceRecord) -> ProvenanceRecord:
        with self._lock, self._db:
            self._db.execute(f"INSERT INTO prov_text ({','.join(_COLS)}) VALUES ({','.join('?' * len(_COLS))})",
                             [rec.origin.value if c == "origin" else getattr(rec, c) for c in _COLS])
        return rec

    def record_text(self, kind: str, text: str, origin: Origin | str, *, parent: str | None = None,
                    model: str | None = None, prompt: str | None = None, context: str | None = None,
                    ) -> ProvenanceRecord:
        origin = Origin(origin)
        if not text.strip():
            raise ProvenanceError("text is empty")
        if origin is Origin.llm_generated and not model:
            raise ProvenanceError("llm_generated text must name the model")
        if origin in (Origin.user_verified, Origin.user_edited):
            raise ProvenanceError(f"{origin.value} records are created by verify_text, not record_text")
        if parent and not self.get(parent):
            raise ProvenanceError(f"unknown parent {parent}")
        return self._insert(ProvenanceRecord(
            id=uuid.uuid4().hex[:12], kind=kind, text=text, origin=origin, model=model,
            prompt_hash=prompt_hash(prompt) if prompt else None, parent_id=parent,
            created_at=datetime.now(UTC).isoformat(timespec="seconds"), context=context))

    def verify_text(self, rid: str, *, edited_text: str | None = None, verified_by: str = "user",
                    ) -> ProvenanceRecord:
        """The user approves LLM text. No edits -> user_verified child; edits -> user_edited child with diff."""
        parent = self.get(rid)
        if not parent:
            raise ProvenanceError(f"unknown record {rid}")
        if parent.origin is not Origin.llm_generated:
            raise ProvenanceError(f"{rid} is {parent.origin.value}; only llm_generated text needs verification")
        if edited_text is not None and not edited_text.strip():
            raise ProvenanceError("edited_text is empty")
        changed = edited_text is not None and edited_text != parent.text
        return self._insert(ProvenanceRecord(
            id=uuid.uuid4().hex[:12], kind=parent.kind, text=edited_text if changed else parent.text,
            origin=Origin.user_edited if changed else Origin.user_verified, model=parent.model,
            prompt_hash=parent.prompt_hash, parent_id=parent.id, context=parent.context,
            created_at=datetime.now(UTC).isoformat(timespec="seconds"), verified_by=verified_by,
            diff_from_parent=_diff(parent.text, edited_text) if changed else None))

    def lineage(self, rid: str, max_depth: int = 20) -> list[ProvenanceRecord]:
        """Record first, then ancestors."""
        out: list[ProvenanceRecord] = []
        cur = self.get(rid)
        while cur and len(out) < max_depth:
            out.append(cur)
            cur = self.get(cur.parent_id) if cur.parent_id else None
        return out

    def list_by_origin(self, origins: frozenset[Origin], kinds: list[str] | None = None) -> list[ProvenanceRecord]:
        q = f"SELECT * FROM prov_text WHERE origin IN ({','.join('?' * len(origins))})"
        args: list = sorted(o.value for o in origins)
        if kinds:
            q += f" AND kind IN ({','.join('?' * len(kinds))})"
            args += kinds
        with self._lock:
            rows = self._db.execute(q + " ORDER BY created_at, id", args).fetchall()
        return [self._row(r) for r in rows]  # type: ignore[misc]

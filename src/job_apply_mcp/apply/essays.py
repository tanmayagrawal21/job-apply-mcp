"""Required-essay autodraft (ADR-0057): local files the owner reads, and labels for plans and run reports.

Every autodrafted essay, approved or not, is saved in full under the ref's evidence folder
(`<data_dir>/evidence/<sha256(ref)[:12]>/essays/<key>.md`) so the owner can read exactly what was filled, or what the
strict reviewer rejected. Only approved text is ever planned as an answer; rejected text never leaves this folder.
"""
from __future__ import annotations

import contextlib
import hashlib
import os
import re
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

RULE = "assumed:essay_autodraft"
_SAFE = re.compile(r"[^A-Za-z0-9_.-]+")


def essay_dir(data_dir: str | Path, ref: str) -> Path:
    return Path(data_dir).expanduser() / "evidence" / hashlib.sha256(ref.encode()).hexdigest()[:12] / "essays"


def save(data_dir: str | Path, ref: str, *, key: str, label: str, company: str, title: str, status: str,
         text: str, violations: list[str] | None = None, tiers: str = "") -> str | None:
    """Write one essay (approved or rejected) as Markdown. Returns the path, or None if it could not be written."""
    try:
        d = essay_dir(data_dir, ref)
        d.mkdir(parents=True, exist_ok=True)
        with contextlib.suppress(OSError):   # owner-only: essays are private prose (no-op on Windows)
            os.chmod(d, 0o700)
        p = d / f"{_SAFE.sub('_', key)[:80] or 'essay'}.md"
        lines = [f"# {label.strip()[:200]}", "",
                 f"- posting: {title} @ {company}", f"- ref: {ref}", f"- status: {status}",
                 f"- generated: {datetime.now(UTC).isoformat(timespec='seconds')}"]
        if tiers:
            lines.append(f"- models: {tiers}")
        if violations:
            lines += ["- review findings:", *[f"  - {v[:300]}" for v in violations[:12]]]
        lines += ["", "---", "", text.strip(), ""]
        fd = os.open(p, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write("\n".join(lines))
        with contextlib.suppress(OSError):
            os.chmod(p, 0o600)               # also tightens a file that existed before with wider bits
        return str(p)
    except OSError:
        return None


def is_essay(source: str) -> bool:
    return source == RULE


def short(value: Any, n: int = 120) -> Any:
    """Compact view of an autodrafted essay for plans and run reports (the full text is in the answers / file)."""
    s = str(value)
    return s if len(s) <= n else s[: n - 1].rstrip() + "…"

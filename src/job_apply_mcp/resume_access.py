"""Is the resume readable by THIS server process? (ADR-0061)

An MCP client launches the server as its own child process. On macOS, TCC (privacy protection) can refuse that
process access to ~/Downloads, ~/Documents or ~/Desktop even though the same user can open the file in Finder, so a
configured `paths.resume` exists but cannot be read. The tool therefore also accepts a resume kept in its own data
directory (`<data_dir>/documents`), which needs no extra permission.

Resolution order: `paths.resume` (a relative path is resolved under the documents dir) if readable; else, ONLY when it
exists but the OS denies access, a same-named copy in the documents dir whose SHA-256 matches the hash recorded the
last time the configured file was readable (an older or different copy is never uploaded silently, ADR-0067); else,
when `paths.resume` is unset, the single `resume.*` file there. A missing configured resume always fails with a fix.
"""
from __future__ import annotations

import errno
import hashlib
import json
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any

_EXTS = (".pdf", ".docx", ".doc", ".txt", ".md")


def documents_dir(settings: Any) -> Path:
    return Path(settings.paths.data_dir).expanduser() / "documents"


@dataclass
class ResumeAccess:
    path: Path | None        # the readable resume to use, or None
    configured: str | None   # paths.resume as written in config
    ok: bool
    why: str = ""
    fix: str = ""

    def compact(self) -> dict[str, Any]:
        d: dict[str, Any] = {"ok": self.ok}
        if self.path is not None:
            d["path"] = str(self.path)
        if self.why:
            d["why"] = self.why
        if self.fix:
            d["fix"] = self.fix
        return d


def readable(p: Path) -> tuple[bool, str]:
    """(True, "") if this process can open and read `p`; else (False, reason). Reads one byte, nothing more."""
    try:
        with p.open("rb") as fh:
            fh.read(1)
        return True, ""
    except FileNotFoundError:
        return False, "not found"
    except IsADirectoryError:
        return False, "is a directory"
    except PermissionError as e:
        return False, "permission denied" + (" (EPERM: blocked by the OS privacy protection)"
                                             if e.errno == errno.EPERM else "")
    except OSError as e:
        return False, e.strerror or type(e).__name__


def _fix(docs: Path, name: str, reason: str) -> str:
    fix = f"Copy the resume into {docs} (e.g. {docs / (name or 'resume.pdf')}) and set paths.resume to that path"
    if "permission" in reason and sys.platform == "darwin":
        fix += ("; or grant Full Disk Access (System Settings > Privacy & Security) to the app that launches "
                "this server (your MCP client)")
    return fix + "."


def resolve_resume(settings: Any) -> ResumeAccess:
    docs = documents_dir(settings)
    conf = settings.paths.resume or None
    if conf:
        p = Path(conf).expanduser()
        if not p.is_absolute():
            p = docs / p
        ok, why = readable(p)
        if ok:
            _record_hash(settings, p)
            return ResumeAccess(p, conf, True)
        alt = docs / p.name
        if "permission" in why and alt != p and readable(alt)[0]:
            if _sha256(alt) == _recorded(settings).get(str(p)):
                return ResumeAccess(alt, conf, True,
                                    f"paths.resume is {why}; using the identical copy in {docs} (hash matches)")
            return ResumeAccess(None, conf, False,
                                f"paths.resume {p} is {why}; the copy in {docs} cannot be verified as the same file",
                                f"Set paths.resume = \"{alt}\" if that copy is your current resume, or grant this "
                                "server's client app access to the original.")
        return ResumeAccess(None, conf, False, f"paths.resume {p} is {why} for this server process",
                            _fix(docs, p.name, why))
    found = sorted(x for x in docs.glob("resume*") if x.suffix.lower() in _EXTS and x.is_file()) \
        if docs.is_dir() else []
    if len(found) == 1 and readable(found[0])[0]:
        return ResumeAccess(found[0], None, True, f"paths.resume not set; using {found[0].name} from {docs}")
    why = ("paths.resume not set" if not found
           else f"paths.resume not set and {len(found)} resume files in {docs}; set one explicitly")
    return ResumeAccess(None, None, False, why, _fix(docs, "resume.pdf", ""))


# ---- recorded hashes: the configured resume's SHA-256, written whenever it was readable --------------------------

def _hash_file(settings: Any) -> Path:
    return documents_dir(settings) / ".resume_hashes.json"


def _sha256(p: Path) -> str | None:
    try:
        return hashlib.sha256(p.read_bytes()).hexdigest()
    except OSError:
        return None


def _recorded(settings: Any) -> dict[str, str]:
    try:
        d = json.loads(_hash_file(settings).read_text(encoding="utf-8"))
        return {str(k): str(v) for k, v in d.items()} if isinstance(d, dict) else {}
    except (OSError, ValueError):
        return {}


def _record_hash(settings: Any, p: Path) -> None:
    h = _sha256(p)
    if h is None:
        return
    rec = _recorded(settings)
    if rec.get(str(p)) == h:
        return
    rec[str(p)] = h
    try:
        f = _hash_file(settings)
        f.parent.mkdir(parents=True, exist_ok=True)
        f.write_text(json.dumps(rec, indent=1), encoding="utf-8")
    except OSError:
        pass     # recording is best effort; without it a blocked resume simply fails with a fix

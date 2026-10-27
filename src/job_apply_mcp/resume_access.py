"""Is the resume readable by THIS server process? (ADR-0061)

An MCP client launches the server as its own child process. On macOS, TCC (privacy protection) can refuse that
process access to ~/Downloads, ~/Documents or ~/Desktop even though the same user can open the file in Finder, so a
configured `paths.resume` exists but cannot be read. The tool therefore also accepts a resume kept in its own data
directory (`<data_dir>/documents`), which needs no extra permission.

Resolution order: `paths.resume` (a relative path is resolved under the documents dir) if readable; else a file with
the same name in the documents dir; else, when `paths.resume` is unset, the single `resume.*` file there.
"""
from __future__ import annotations

import errno
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
            return ResumeAccess(p, conf, True)
        alt = docs / p.name
        if alt != p and readable(alt)[0]:
            return ResumeAccess(alt, conf, True, f"paths.resume is {why}; using the copy in {docs}")
        return ResumeAccess(None, conf, False, f"paths.resume {p} is {why} for this server process",
                            _fix(docs, p.name, why))
    found = sorted(x for x in docs.glob("resume*") if x.suffix.lower() in _EXTS and x.is_file()) \
        if docs.is_dir() else []
    if len(found) == 1 and readable(found[0])[0]:
        return ResumeAccess(found[0], None, True, f"paths.resume not set; using {found[0].name} from {docs}")
    why = ("paths.resume not set" if not found
           else f"paths.resume not set and {len(found)} resume files in {docs}; set one explicitly")
    return ResumeAccess(None, None, False, why, _fix(docs, "resume.pdf", ""))

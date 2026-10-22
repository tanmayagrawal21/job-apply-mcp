"""Compact context pack for the coder: module map, Protocols, a sibling template, ADRs, run evidence."""
from __future__ import annotations

import ast
import re
from pathlib import Path

# kind -> (directory under the package holding sibling implementations, glob)
SIBLING_DIRS = {"platform_adapter": "platforms", "control": "policy", "config": "", "new_tool": "", "bug": ""}
_SKIP = {"__init__.py", "__pycache__"}


def _doc1(path: Path) -> str:
    try:
        doc = ast.get_docstring(ast.parse(path.read_text(encoding="utf-8")))
    except (SyntaxError, OSError):
        return ""
    return (doc or "").strip().splitlines()[0][:100] if doc else ""


def module_map(repo: Path, pkg: str = "src/job_apply_mcp", limit: int = 60) -> list[str]:
    root = repo / pkg
    rows = []
    for p in sorted(root.rglob("*.py")):
        if p.name == "__init__.py" and not _doc1(p):
            continue
        rows.append(f"{p.relative_to(repo)}: {_doc1(p)}")
    return rows[:limit]


def protocols(repo: Path, pkg: str = "src/job_apply_mcp") -> list[str]:
    f = repo / pkg / "core" / "interfaces.py"
    if not f.is_file():
        return []
    tree = ast.parse(f.read_text(encoding="utf-8"))
    out = []
    for n in tree.body:
        if isinstance(n, ast.ClassDef):
            meths = [m.name for m in n.body if isinstance(m, (ast.FunctionDef, ast.AsyncFunctionDef))]
            out.append(f"{n.name}({', '.join(meths)})")
    return out


def _tokens(text: str) -> set[str]:
    return {t for t in re.findall(r"[a-z][a-z0-9_]{3,}", text.lower())} - {"that", "with", "this", "from", "when", "should"}


def relevant_files(repo: Path, description: str, limit: int = 6) -> list[str]:
    toks = _tokens(description)
    scored = []
    for p in (repo / "src").rglob("*.py"):
        try:
            body = p.read_text(encoding="utf-8").lower()
        except OSError:
            continue
        s = sum(3 for t in toks if t in p.as_posix().lower()) + sum(1 for t in toks if t in body)
        if s:
            scored.append((s, str(p.relative_to(repo))))
    return [f for _, f in sorted(scored, reverse=True)[:limit]]


def sibling_template(repo: Path, kind: str, pkg: str = "src/job_apply_mcp", max_lines: int = 60) -> dict | None:
    sub = SIBLING_DIRS.get(kind, "")
    cands = [p for p in sorted((repo / pkg / sub).glob("*.py")) if p.name not in _SKIP] if sub else []
    if not cands:
        fallback = {"new_tool": "server.py", "config": "config.py", "bug": ""}.get(kind, "")
        if not fallback:
            return None
        cands = [repo / pkg / fallback]
    p = cands[0]
    lines = p.read_text(encoding="utf-8").splitlines()[:max_lines]
    return {"path": str(p.relative_to(repo)), "head": "\n".join(lines)}


def related_adrs(repo: Path, description: str, limit: int = 3) -> list[str]:
    toks = _tokens(description)
    scored = []
    for p in sorted((repo / "docs" / "adr").glob("*.md")):
        text = p.read_text(encoding="utf-8")
        title = text.splitlines()[0].lstrip("# ").strip() if text else p.stem
        s = sum(1 for t in toks if t in text.lower()) + 5 * sum(1 for t in toks if t in title.lower())
        scored.append((s, f"{p.name}: {title}"))
    hits = [x for s, x in sorted(scored, reverse=True) if s][:limit]
    return hits


def evidence_for(props: list, description: str, limit: int = 3) -> list[str]:
    toks = _tokens(description)
    out = [p.title for p in props if toks & _tokens(p.title + " " + p.platform)]
    return out[:limit]


def build_pack(repo: Path, kind: str, description: str, props: list, max_chars: int) -> dict:
    pack = {
        "modules": module_map(repo), "protocols": protocols(repo),
        "relevant_files": relevant_files(repo, description), "template": sibling_template(repo, kind),
        "adrs": related_adrs(repo, description), "evidence": evidence_for(props, description),
    }
    # Trim the largest parts until under budget (modules first, then template body).
    def size() -> int:
        return len(str(pack))
    while size() > max_chars and pack["modules"]:
        pack["modules"] = pack["modules"][: max(0, len(pack["modules"]) // 2)]
    if size() > max_chars and pack["template"]:
        pack["template"]["head"] = pack["template"]["head"][: max(200, max_chars // 3)]
    return pack

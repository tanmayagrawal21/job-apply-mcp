#!/usr/bin/env python3
"""Dependency-free privacy/secret scanner for a public repo (stdlib only).

Modes: --diff [BASE] (added lines only), --staged, --paths P [P ...], or no mode = whole tracked tree.
Private terms (--terms FILE, one per line, case-insensitive) are never printed; findings show
"private term #k" and a masked excerpt. Exit 1 when anything is found.
"""
from __future__ import annotations

import argparse
import math
import re
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path

ALLOW_FILE = ".privacy-allow"
SAFE_EMAIL_DOMAINS = {"example.com", "example.org", "example.net", "localhost", "users.noreply.github.com",
                      "noreply.github.com", "anthropic.com"}
SAFE_EMAIL_SUFFIXES = (".example", ".test", ".invalid", ".localhost", ".example.com", ".example.org")
SAFE_EMAIL_LOCALS = {"noreply", "no-reply"}
PERSON_FILE_EXT = {".eml", ".mbox", ".pdf", ".docx"}
FIXTURE_DIRS = ("tests/", "fixtures/", "examples/")
BINARY_EXT = {".png", ".jpg", ".jpeg", ".gif", ".ico", ".zip", ".gz", ".whl", ".lock", ".pyc"}

EMAIL_RE = re.compile(r"(?<![\w.+-])[A-Za-z0-9._%+-]+@([A-Za-z0-9-]+(?:\.[A-Za-z0-9-]+)+)\b")
PHONE_RE = re.compile(
    r"(?<![\w.])(?:\+\d{1,3}[\s.-]?)?(?:\(\d{3}\)\s?|\d{3}[\s.-])\d{3}[\s.-]\d{4}(?![\w])"  # US-style
    r"|(?<![\w.])\+\d{1,3}(?:[\s.-]?\d{2,5}){2,4}(?![\w])"  # international
)
STREET_RE = re.compile(
    r"\b\d{1,6}\s+(?:[NSEW]\.?\s+)?(?:[A-Z][a-z]+\s+){1,3}"
    r"(?:Street|St|Avenue|Ave|Road|Rd|Boulevard|Blvd|Drive|Dr|Lane|Ln|Way|Court|Ct|Place|Pl|Parkway|Pkwy)\b\.?"
)
HOME_RE = re.compile(r"(?:/Users|/home)/(?!Shared\b|runner\b|user\b|me\b|you\b|<|\$|\{|\*|name\b|username\b)([A-Za-z0-9._-]+)")
SECRET_RES = [
    ("openai/anthropic-style key", re.compile(r"\bsk-[A-Za-z0-9_-]{20,}")),
    ("github token", re.compile(r"\b(?:gh[pousr]_[A-Za-z0-9]{30,}|github_pat_[A-Za-z0-9_]{30,})")),
    ("aws access key id", re.compile(r"\b(?:AKIA|ASIA)[0-9A-Z]{16}\b")),
    ("slack token", re.compile(r"\bxox[abprs]-[A-Za-z0-9-]{10,}")),
    ("private key header", re.compile(r"-----BEGIN (?:[A-Z]+ )*PRIVATE KEY(?: BLOCK)?-----")),
]
KV_RE = re.compile(
    r"""(?ix)\b([A-Za-z0-9_.-]*(?:key|secret|token|passw(?:or)?d|pwd|credential)[A-Za-z0-9_.-]*)\s*[:=]\s*
        ["']?([A-Za-z0-9+/=_\-.]{20,})["']?""")


@dataclass
class Finding:
    path: str
    line: int
    kind: str
    excerpt: str

    def __str__(self) -> str:
        return f"{self.path}:{self.line}: [{self.kind}] {self.excerpt}"


def entropy(s: str) -> float:
    if not s:
        return 0.0
    return -sum((s.count(c) / len(s)) * math.log2(s.count(c) / len(s)) for c in set(s))


def load_allow(root: Path) -> list[re.Pattern]:
    f = root / ALLOW_FILE
    pats: list[re.Pattern] = []
    if f.exists():
        for raw in f.read_text(encoding="utf-8").splitlines():
            raw = raw.strip()
            if raw and not raw.startswith("#"):
                pats.append(re.compile(raw))
    return pats


def load_terms(path: str | None) -> list[str]:
    if not path:
        return []
    p = Path(path).expanduser()
    if not p.exists():
        return []
    return [t.strip().lower() for t in p.read_text(encoding="utf-8").splitlines()
            if t.strip() and not t.startswith("#")]


def email_ok(m: re.Match) -> bool:
    dom = m.group(1).lower()
    local = m.group(0).split("@")[0].lower()
    return (dom in SAFE_EMAIL_DOMAINS or dom.endswith(SAFE_EMAIL_SUFFIXES) or local in SAFE_EMAIL_LOCALS
            or dom.rsplit(".", 1)[-1] in {"test", "invalid", "example", "localhost"})


def phone_ok(m: re.Match) -> bool:
    digits = re.sub(r"\D", "", m.group(0))
    if not 10 <= len(digits) <= 15:
        return False
    # fictional 555-01xx block, all-same or sequential digits, and ISO dates/versions are not phones
    if re.search(r"555[\s.-]?01\d\d\D*$", m.group(0)) or len(set(digits)) <= 2 or digits in "01234567890123456789":
        return False
    return not re.fullmatch(r"\d{4}-\d{2}-\d{2}", m.group(0))


def mask_spans(line: str, spans: list[tuple[int, int]]) -> str:
    out, last = [], 0
    for a, b in sorted(spans):
        if a < last:
            continue
        out += [line[last:a], "***"]
        last = b
    out.append(line[last:])
    s = "".join(out).strip()
    return s if len(s) <= 100 else s[:97] + "..."


def scan_line(line: str, terms: list[str]) -> list[tuple[str, list[tuple[int, int]]]]:
    hits: list[tuple[str, list[tuple[int, int]]]] = []
    low = line.lower()
    for k, t in enumerate(terms, 1):
        for m in re.finditer(re.escape(t), low):
            hits.append((f"private term #{k}", [m.span()]))
    for m in EMAIL_RE.finditer(line):
        if not email_ok(m):
            hits.append(("email address", [m.span()]))
    for m in PHONE_RE.finditer(line):
        if phone_ok(m):
            hits.append(("phone number", [m.span()]))
    for m in STREET_RE.finditer(line):
        hits.append(("street address", [m.span()]))
    for m in HOME_RE.finditer(line):
        hits.append(("absolute home path", [m.span(1)]))
    for name, rx in SECRET_RES:
        for m in rx.finditer(line):
            hits.append((f"secret: {name}", [m.span()]))
    for m in KV_RE.finditer(line):
        v = m.group(2)
        if entropy(v) >= 3.5 and not re.fullmatch(r"[A-Za-z_.-]+", v):
            hits.append(("secret: high-entropy key=value", [m.span(2)]))
    return hits


def scan_text(path: str, lines: list[tuple[int, str]], terms: list[str], allow: list[re.Pattern]) -> list[Finding]:
    out: list[Finding] = []
    for no, line in lines:
        if len(line) > 2000:
            line = line[:2000]
        hits = scan_line(line, terms)
        if not hits:
            continue
        if any(p.search(f"{path}:{line}") or p.search(path) for p in allow):
            continue
        spans = [s for _, sp in hits for s in sp]
        ex = mask_spans(line, spans)
        seen = set()
        for kind, _ in hits:
            if kind not in seen:
                seen.add(kind)
                out.append(Finding(path, no, kind, ex))
    return out


def check_file_type(path: str, allow: list[re.Pattern], added: bool = True) -> list[Finding]:
    ext = Path(path).suffix.lower()
    if ext in PERSON_FILE_EXT and not path.startswith(FIXTURE_DIRS) and not any(p.search(path) for p in allow):
        return [Finding(path, 0, "real-person data file type", f"{ext} outside tests/fixtures")]
    return []


def git(root: Path, *args: str) -> str:
    r = subprocess.run(["git", "-C", str(root), *args], capture_output=True, text=True, check=False)
    if r.returncode != 0:
        raise SystemExit(f"git {' '.join(args)} failed: {r.stderr.strip()}")
    return r.stdout


def parse_diff(diff: str) -> dict[str, list[tuple[int, str]]]:
    res: dict[str, list[tuple[int, str]]] = {}
    cur, no = None, 0
    for ln in diff.splitlines():
        if ln.startswith("+++ "):
            cur = None if ln.endswith("/dev/null") else ln[6:]
            if cur is not None:
                res.setdefault(cur, [])
        elif ln.startswith("@@"):
            m = re.search(r"\+(\d+)", ln)
            no = int(m.group(1)) if m else 0
        elif cur and ln.startswith("+") and not ln.startswith("+++"):
            res[cur].append((no, ln[1:]))
            no += 1
    return res


def read_lines(p: Path) -> list[tuple[int, str]] | None:
    if p.suffix.lower() in BINARY_EXT:
        return None
    try:
        data = p.read_bytes()
    except OSError:
        return None
    if b"\0" in data[:4096]:
        return None
    return list(enumerate(data.decode("utf-8", "replace").splitlines(), 1))


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    g = ap.add_mutually_exclusive_group()
    g.add_argument("--diff", nargs="?", const="origin/main", metavar="BASE", help="scan lines added vs BASE")
    g.add_argument("--staged", action="store_true", help="scan staged additions (pre-commit)")
    g.add_argument("--paths", nargs="+", metavar="PATH", help="scan these files/dirs")
    ap.add_argument("--terms", metavar="FILE", help="private terms file (never printed)")
    ap.add_argument("--root", default=".", help="repo root (default: cwd)")
    a = ap.parse_args(argv)

    root = Path(git(Path(a.root), "rev-parse", "--show-toplevel").strip())
    allow, terms = load_allow(root), load_terms(a.terms)
    if a.terms and not Path(a.terms).expanduser().exists():
        print(f"note: terms file {a.terms} not found; skipping private terms", file=sys.stderr)
    findings: list[Finding] = []

    if a.diff is not None or a.staged:
        if a.staged:
            diff = git(root, "diff", "--cached", "-U0", "--no-color", "--no-ext-diff", "--no-textconv")
            added = git(root, "diff", "--cached", "--name-only", "--diff-filter=A").split()
        else:
            base = git(root, "merge-base", a.diff, "HEAD").strip()
            diff = git(root, "diff", "-U0", "--no-color", "--no-ext-diff", "--no-textconv", base)
            added = git(root, "diff", "--name-only", "--diff-filter=A", base).split()
        for path, lines in parse_diff(diff).items():
            findings += scan_text(path, lines, terms, allow)
        for path in added:
            findings += check_file_type(path, allow)
    else:
        if a.paths:
            files = []
            for p in a.paths:
                pp = Path(p)
                files += [f for f in pp.rglob("*") if f.is_file() and ".git" not in f.parts] if pp.is_dir() else [pp]
        else:
            files = [root / f for f in git(root, "ls-files").splitlines()]
        for f in files:
            try:
                rel = str(f.resolve().relative_to(root))
            except ValueError:
                rel = str(f)
            if rel == ALLOW_FILE:
                continue
            findings += check_file_type(rel, allow)
            lines = read_lines(f)
            if lines is not None:
                findings += scan_text(rel, lines, terms, allow)

    for f in findings:
        print(f)
    if findings:
        print(f"\n{len(findings)} privacy finding(s). Fix, or allowlist fictional fixtures in {ALLOW_FILE}.",
              file=sys.stderr)
        return 1
    print("privacy scan clean")
    return 0


if __name__ == "__main__":
    sys.exit(main())

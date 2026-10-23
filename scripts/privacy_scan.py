#!/usr/bin/env python3
"""Dependency-free privacy/secret scanner for a public repo (stdlib only).

Modes: --diff [BASE] (added lines only), --staged, --paths P [P ...], or no mode = whole tracked tree.
Private terms (--terms FILE, one per line, case-insensitive) are never printed; findings show
"private term #k" and a masked excerpt.

--allow FILE (default ~/.config/job-apply-mcp/privacy_allow.txt when it exists; kept outside the repo)
accepts known, owner-approved findings in whole-tree scans only (--diff/--staged always report added lines).
Lines: `@salt <random>`, `<path-glob>\t<fingerprint-or-*>` (fingerprint = --fingerprint K, a salted SHA-256
prefix of term #K, so rules survive reordering and hold no value), or `@urls` (a private term inside the owner
segment of https://github.com/<owner>[/...], nowhere else in the URL). Accepted findings are counted, never printed.
Exit 0 when every finding is accepted, 1 otherwise.
"""
from __future__ import annotations

import argparse
import base64
import binascii
import fnmatch
import hashlib
import math
import re
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path

ALLOW_FILE = ".privacy-allow"
DEFAULT_ACCEPT_FILE = Path("~/.config/job-apply-mcp/privacy_allow.txt")
GITHUB_OWNER_RE = re.compile(r"https://github\.com/([A-Za-z0-9-]{1,39})(?=[/\s)\]>\"'`.,]|$)", re.IGNORECASE)
FP_RE = re.compile(r"[0-9a-f]{12}")
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
    accepted: bool = False

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


@dataclass
class AcceptRules:
    """Owner-approved findings, loaded from a file outside the repo. Holds no term values."""
    pairs: list[tuple[str, str]]  # (path glob, term fingerprint or "*")
    urls: bool = False
    salt: str = ""
    fps: tuple[str, ...] = ()  # fingerprint of term #k at index k-1, set by bind()

    def bind(self, terms: list[str]) -> None:
        self.fps = tuple(fingerprint(self.salt, t) for t in terms) if self.salt else ()

    def accepts(self, path: str, kind: str, span: tuple[int, int], line: str) -> bool:
        m = re.fullmatch(r"private term #(\d+)", kind)
        if not m:
            return False
        if self.urls and any(u.start(1) <= span[0] and span[1] <= u.end(1) for u in GITHUB_OWNER_RE.finditer(line)):
            return True
        k = int(m.group(1))
        fp = self.fps[k - 1] if 0 < k <= len(self.fps) else None
        return any(fnmatch.fnmatchcase(path, g) and (t == "*" or t == fp) for g, t in self.pairs)


def fingerprint(salt: str, term: str) -> str:
    """Short salted SHA-256 prefix of a term: stable across reordering, not reversible without the salt."""
    return hashlib.sha256(f"{salt}\0{term.strip().lower()}".encode()).hexdigest()[:12]


class AllowFileError(Exception):
    """The allow file is unsafe to use (inside a git work tree, or a weak salt). Message holds no secret."""


MIN_SALT_BYTES = 16


def salt_ok(salt: str) -> bool:
    """At least 16 bytes as hex or base64, and not an obviously non-random string."""
    if len(set(salt)) < 8:
        return False
    if re.fullmatch(r"[0-9a-fA-F]+", salt) and len(salt) % 2 == 0:
        return len(salt) // 2 >= MIN_SALT_BYTES
    std = salt.replace("-", "+").replace("_", "/")  # accept url-safe base64 too
    try:
        return len(base64.b64decode(std + "=" * (-len(std) % 4), validate=True)) >= MIN_SALT_BYTES
    except (binascii.Error, ValueError):
        return False


def in_git_worktree(p: Path) -> bool:
    try:
        r = subprocess.run(["git", "-C", str(p.parent), "rev-parse", "--is-inside-work-tree"],
                           capture_output=True, text=True, check=False)
    except OSError:
        return False
    return r.returncode == 0 and r.stdout.strip() == "true"


def load_accept(path: str | None) -> AcceptRules | None:
    p = Path(path).expanduser() if path else DEFAULT_ACCEPT_FILE.expanduser()
    if not p.exists():
        if path:
            print(f"note: allow file {path} not found; no findings accepted", file=sys.stderr)
        return None
    if in_git_worktree(p.resolve()):
        raise AllowFileError(f"allow file {p} is inside a git work tree; keep it outside every repo "
                             "(its salt makes fingerprints guessable if published)")
    rules = AcceptRules([])
    for no, raw in enumerate(p.read_text(encoding="utf-8").splitlines(), 1):
        raw = raw.strip()
        if not raw or raw.startswith("#"):
            continue
        if raw == "@urls":
            rules.urls = True
            continue
        if raw.startswith("@salt"):
            rules.salt = raw[5:].strip()
            if not salt_ok(rules.salt):
                raise AllowFileError(f"allow file line {no}: salt must be at least {MIN_SALT_BYTES} random bytes "
                                     "as hex or base64 (e.g. python -c 'import secrets;print(secrets.token_hex(16))')")
            continue
        parts = raw.split("\t")
        if len(parts) != 2 or not (parts[1].strip() == "*" or FP_RE.fullmatch(parts[1].strip())):
            print(f"note: allow file line {no} ignored (expected <path-glob><TAB><fingerprint-or-*>)", file=sys.stderr)
            continue
        rules.pairs.append((parts[0].strip(), parts[1].strip()))
    if not rules.salt and any(t != "*" for _, t in rules.pairs):
        raise AllowFileError("allow file has fingerprint rules but no @salt line")
    return rules


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
    merged: list[list[int]] = []
    for a, b in sorted(spans):
        if merged and a <= merged[-1][1]:
            merged[-1][1] = max(merged[-1][1], b)
        else:
            merged.append([a, b])
    out, last = [], 0
    for a, b in merged:
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


def scan_text(path: str, lines: list[tuple[int, str]], terms: list[str], allow: list[re.Pattern],
              accept: AcceptRules | None = None) -> list[Finding]:
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
        status: dict[str, bool] = {}
        for kind, sp in hits:
            ok = accept is not None and all(accept.accepts(path, kind, s, line) for s in sp)
            status[kind] = status.get(kind, True) and ok
        out += [Finding(path, no, kind, ex, ok) for kind, ok in status.items()]
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
    ap.add_argument("--allow", metavar="FILE",
                    help=f"accepted-findings file outside the repo (default {DEFAULT_ACCEPT_FILE} if present)")
    ap.add_argument("--fingerprint", type=int, metavar="K",
                    help="print the allow-file fingerprint of private term #K (needs --terms and an @salt line)")
    ap.add_argument("--root", default=".", help="repo root (default: cwd)")
    a = ap.parse_args(argv)

    root = Path(git(Path(a.root), "rev-parse", "--show-toplevel").strip())
    try:
        accept = load_accept(a.allow)
    except AllowFileError as e:
        print(f"error: {e}", file=sys.stderr)
        return 2
    allow, terms = load_allow(root), load_terms(a.terms)
    if accept is not None:
        accept.bind(terms)
    if a.fingerprint is not None:
        if accept is None or not accept.salt or not 0 < a.fingerprint <= len(terms):
            print("error: --fingerprint needs --terms, an allow file with an @salt line, and a valid K", file=sys.stderr)
            return 2
        print(accept.fps[a.fingerprint - 1])
        return 0
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
            findings += scan_text(path, lines, terms, allow)  # added lines: never auto-accepted
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
                findings += scan_text(rel, lines, terms, allow, accept)

    open_ = [f for f in findings if not f.accepted]
    n_ok = len(findings) - len(open_)
    for f in open_:
        print(f)
    note = f" ({n_ok} accepted)" if n_ok else ""
    if open_:
        print(f"\n{len(open_)} privacy finding(s){note}. Fix, or allowlist fictional fixtures in {ALLOW_FILE}.",
              file=sys.stderr)
        return 1
    print(f"privacy scan clean{note}")
    return 0


if __name__ == "__main__":
    sys.exit(main())

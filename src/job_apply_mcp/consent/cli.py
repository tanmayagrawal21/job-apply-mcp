"""HUMAN-ONLY: `job-apply-mcp consent --for 24h | --per-run [--max N] [--scopes ...] | --status | --revoke [id|all]`
(ADR-0048). Minting needs an interactive terminal and the exact confirmation phrase."""
from __future__ import annotations

import argparse
import sys
import time
from typing import Any

from ..grants.cli import _tty, fmt_dur
from ..grants.store import os_user, parse_duration
from .store import CONSENT_SCOPES, DEFAULT_SCOPES, ConsentStore, store_for


def _scopes(raw: list[str] | None) -> list[str]:
    if not raw:
        return list(DEFAULT_SCOPES)
    out = [s.strip() for item in raw for s in item.split(",") if s.strip()]
    bad = sorted(set(out) - set(CONSENT_SCOPES))
    if bad:
        raise ValueError(f"unknown scope(s) {bad}; known: {', '.join(sorted(CONSENT_SCOPES))}")
    return sorted(set(out))


def cli(argv: list[str], settings: Any, *, stdin: Any = None, stdout: Any = None, input_fn: Any = input,
        store: ConsentStore | None = None) -> int:
    stdin, stdout = stdin or sys.stdin, stdout or sys.stdout
    p = argparse.ArgumentParser(prog="job-apply-mcp consent", description=__doc__)
    when = p.add_mutually_exclusive_group()
    when.add_argument("--for", dest="duration", help="window length, e.g. 24h, 7d, 3h30m")
    when.add_argument("--per-run", action="store_true", help="cover one run only")
    when.add_argument("--status", action="store_true")
    when.add_argument("--revoke", nargs="?", const="all", metavar="ID|all")
    p.add_argument("--max", type=int, default=None, help="at most N submissions under this consent")
    p.add_argument("--scopes", nargs="+", metavar="SCOPE",
                   help=f"subset of: {', '.join(DEFAULT_SCOPES)} (default: all)")
    try:
        a = p.parse_args(argv)
    except SystemExit as e:
        return int(e.code or 2)
    st = store or store_for(settings)
    g = settings.grants

    if a.status:
        rows = st.status()
        for r in rows:
            left = "unlimited" if r["submissions_left"] is None else r["submissions_left"]
            print(f"{r['id']}  {r['mode']}  expires in {fmt_dur(r['expires_in_s'])}  submissions left: {left}  "
                  f"scopes: {','.join(r['scopes'])}")
        if not rows:
            print("no active consent")
        return 0
    if a.revoke is not None:
        gone = st.revoke(a.revoke)
        print(f"revoked: {', '.join(gone) if gone else 'nothing matched'}")
        return 0 if gone or a.revoke == "all" else 1
    if not a.duration and not a.per_run:
        print(__doc__)
        return 2
    if not _tty(stdin, stdout):
        print("refused: `consent` is human-only and needs an interactive terminal (stdin and stdout must be a TTY)")
        return 3
    try:
        scopes = _scopes(a.scopes)
        secs = g.consent_per_run_max_s if a.per_run else parse_duration(a.duration)
    except ValueError as e:
        print(f"consent: {e}")
        return 2
    cap = g.consent_max_duration_s
    if secs <= 0 or secs > cap:
        print(f"consent: duration must be between 1s and {fmt_dur(cap)} (grants.consent_max_duration_s)")
        return 2
    if a.max is not None and not 1 <= a.max <= g.max_uses_cap:
        print(f"consent: --max must be 1..{g.max_uses_cap}")
        return 2
    span = "this run" if a.per_run else fmt_dur(secs)
    print("Unattended applying. Inside this consent nothing asks you first; review the results afterwards.\n")
    for s in scopes:
        print(f"  {s}\n      {CONSENT_SCOPES[s]}")
    print(f"\nCovers:      {span}" + (f" (and at most {fmt_dur(secs)})" if a.per_run else "")
          + f"\nSubmissions: {a.max if a.max else 'unlimited (your daily/run caps still apply)'}"
          + f"\nOS user:     {os_user()}\nUnchanged:   dry_run, policy gates, stop phrases, caps, dedupe. "
          "A visible CAPTCHA is always handed to you.\nRevoke:      job-apply-mcp consent --revoke")
    phrase = f"I consent to unattended applying for {span}"
    if input_fn(f'\nType exactly "{phrase}" to confirm: ').strip() != phrase:
        print("consent: confirmation did not match; nothing changed")
        return 1
    c = st.issue_consent(secs, scopes=scopes, mode="per_run" if a.per_run else "window", max_uses=a.max)
    print(f"consent {c.id} active until {time.strftime('%Y-%m-%d %H:%M', time.localtime(c.expires))}")
    return 0

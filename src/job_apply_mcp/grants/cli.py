"""HUMAN-ONLY: `job-apply-mcp grant <scope> --for <duration> [--max-uses N] | --list | --revoke [id|all]` (ADR-0027)."""
from __future__ import annotations

import argparse
import sys
import time
from typing import Any

from .store import SCOPES, GrantStore, os_user, parse_duration


def _tty(stdin: Any, stdout: Any) -> bool:
    return bool(getattr(stdin, "isatty", lambda: False)() and getattr(stdout, "isatty", lambda: False)())


def fmt_dur(s: float) -> str:
    s = int(max(0, s))
    h, r = divmod(s, 3600)
    m, sec = divmod(r, 60)
    return f"{h}h{m:02d}m" if h else (f"{m}m{sec:02d}s" if m else f"{sec}s")


def cli(argv: list[str], settings: Any, *, stdin: Any = None, stdout: Any = None, input_fn: Any = input,
        store: GrantStore | None = None) -> int:
    stdin, stdout = stdin or sys.stdin, stdout or sys.stdout
    p = argparse.ArgumentParser(prog="job-apply-mcp grant", description=__doc__)
    p.add_argument("scope", nargs="?", choices=sorted(SCOPES))
    p.add_argument("--for", dest="duration", help="e.g. 30m, 2h, 1h30m")
    p.add_argument("--max-uses", type=int, default=None)
    p.add_argument("--list", action="store_true")
    p.add_argument("--revoke", nargs="?", const="all", metavar="ID|all")
    try:
        a = p.parse_args(argv)
    except SystemExit as e:
        return int(e.code or 2)
    st = store or GrantStore(settings.paths.data_dir, use_keyring=settings.grants.use_keyring)

    if a.list:
        rows = st.active()
        for g, left in rows:
            print(f"{g.id}  {g.scope}  expires in {fmt_dur(g.expires - time.time())}  "
                  f"uses left: {'unlimited' if left is None else left}")
        if not rows:
            print("no active grants")
        return 0
    if a.revoke is not None:
        gone = st.revoke(a.revoke)
        print(f"revoked: {', '.join(gone) if gone else 'nothing matched'}")
        return 0 if gone or a.revoke == "all" else 1
    if not a.scope or not a.duration:
        print("usage: job-apply-mcp grant <scope> --for <duration> [--max-uses N] | --list | --revoke [id|all]\n"
              f"scopes: {', '.join(sorted(SCOPES))}")
        return 2
    if not _tty(stdin, stdout):
        print("refused: `grant` is human-only and needs an interactive terminal (stdin and stdout must be a TTY)")
        return 3
    try:
        secs = parse_duration(a.duration)
    except ValueError as e:
        print(f"grant: {e}")
        return 2
    cap = settings.grants.max_duration_s
    if secs <= 0 or secs > cap:
        print(f"grant: duration must be between 1s and {fmt_dur(cap)} (grants.max_duration_s)")
        return 2
    if a.max_uses is not None and not 1 <= a.max_uses <= settings.grants.max_uses_cap:
        print(f"grant: --max-uses must be 1..{settings.grants.max_uses_cap}")
        return 2
    print(f"Scope:     {a.scope}\nAllows:    {SCOPES[a.scope]}\nDuration:  {fmt_dur(secs)}\n"
          f"Max uses:  {a.max_uses if a.max_uses else 'unlimited within the window'}\nOS user:   {os_user()}")
    phrase = f"I authorize {a.scope} for {fmt_dur(secs)}"
    if input_fn(f'Type exactly "{phrase}" to confirm: ').strip() != phrase:
        print("grant: confirmation did not match; nothing changed")
        return 1
    g = st.issue(a.scope, secs, a.max_uses)
    print(f"granted {g.id} ({g.scope}) until {time.strftime('%H:%M', time.localtime(g.expires))}")
    return 0

"""`job-apply-mcp profile ask|list|answer`: build the profile from the terminal (ADR-0047)."""
from __future__ import annotations

import argparse
import sys
from typing import Any

from .tools import db_for, questions, store_answer


def _show(rows: list[dict[str, Any]]) -> None:
    for r in rows:
        d = f"  [default: {r['default_used']}]" if r.get("default_used") else ""
        print(f"{r['key']}: {r['question']}{d}\n    seen on {r['n']} application(s), {r['blocked']} blocked; "
              f"e.g. {'; '.join(r['examples'][:2])}")


def cli(argv: list[str], settings: Any, *, input_fn=input, is_tty: bool | None = None) -> int:
    p = argparse.ArgumentParser(prog="job-apply-mcp profile")
    sub = p.add_subparsers(dest="cmd", required=True)
    a = sub.add_parser("ask", help="answer the top open gaps interactively (needs a terminal)")
    a.add_argument("--limit", type=int, default=None)
    sub.add_parser("list", help="show the top open gaps").add_argument("--limit", type=int, default=10)
    s = sub.add_parser("answer", help="store one answer")
    s.add_argument("key")
    s.add_argument("value")
    s.add_argument("--scope", default="global", help="global | company:<name> | role_family:<family>")
    args = p.parse_args(argv)
    if args.cmd == "list":
        rows = questions(settings, args.limit)
        _show(rows) if rows else print("no open gaps")
        return 0
    tty = sys.stdin.isatty() if is_tty is None else is_tty
    if args.cmd == "answer":
        if not tty:
            print("profile answer is stored as written by you only from a terminal.", file=sys.stderr)
            return 2
        try:
            print(store_answer(settings, args.key, args.value, args.scope, origin="user_written"))
        except ValueError as e:
            print(f"error: {e}", file=sys.stderr)
            return 2
        return 0
    if not tty:
        print("profile ask needs an interactive terminal; use `profile list` and `profile answer` instead.",
              file=sys.stderr)
        return 2
    rows = questions(settings, args.limit or settings.persona.ask_limit)
    if not rows:
        print("no open gaps")
        return 0
    db, done = db_for(settings), 0
    print("Enter an answer, or press Enter to skip. Type 'quit' to stop. Answers are saved and reused.\n")
    for r in rows:
        _show([r])
        try:
            val = input_fn("> ").strip()
        except (EOFError, KeyboardInterrupt):
            print()
            break
        if val.lower() == "quit":
            break
        if val:
            db.set_answer(r["key"], val, "global", origin="user_written")
            done += 1
        else:
            db.set_status(r["key"], "skipped")
    print(f"saved {done} answer(s)")
    return 0

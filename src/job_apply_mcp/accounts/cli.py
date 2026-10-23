"""HUMAN-ONLY: `job-apply-mcp vault add <host> --username U | list | rm <host> [--username U]` (ADR-0046).

Every subcommand needs an interactive terminal. `add` prompts for the password with no echo (getpass) and asks
for it twice. Nothing here prints, logs or returns a secret.
"""
from __future__ import annotations

import argparse
import getpass
import sys
from typing import Any

from ..core.secrets import Secret
from .vault import Backend, Vault, VaultError, normalize_host


def _tty(stdin: Any, stdout: Any) -> bool:
    return bool(getattr(stdin, "isatty", lambda: False)() and getattr(stdout, "isatty", lambda: False)())


def cli(argv: list[str], settings: Any, *, stdin: Any = None, stdout: Any = None,
        getpass_fn: Any = getpass.getpass, backend: Backend | None = None) -> int:
    stdin, stdout = stdin or sys.stdin, stdout or sys.stdout
    p = argparse.ArgumentParser(prog="job-apply-mcp vault", description=__doc__)
    sub = p.add_subparsers(dest="op")
    a = sub.add_parser("add", help="store a login for an ATS host in the OS keychain")
    a.add_argument("host")
    a.add_argument("--username", "-u", required=True)
    sub.add_parser("list", help="hosts and usernames (never passwords)")
    r = sub.add_parser("rm", help="delete a stored login")
    r.add_argument("host")
    r.add_argument("--username", "-u")
    try:
        args = p.parse_args(argv)
    except SystemExit as e:
        return int(e.code or 2)
    if not args.op:
        p.print_usage()
        return 2
    if not _tty(stdin, stdout):
        print("refused: `vault` is human-only and needs an interactive terminal (stdin and stdout must be a TTY)")
        return 3
    v = Vault.from_settings(settings, backend)
    try:
        if args.op == "list":
            rows = v.entries()
            for e in rows:
                print(f"{e['host']}  {e['username']}  added {e['added_at']}")
            if not rows:
                print("vault is empty")
            return 0
        host = normalize_host(args.host)
        if args.op == "rm":
            gone = v.remove(host, args.username)
            print(f"removed: {', '.join(gone)} @ {host}" if gone else f"nothing stored for {host}")
            return 0 if gone else 1
        pw = getpass_fn(f"Password for {args.username} @ {host} (not echoed): ")
        if not pw:
            print("vault: empty password; nothing stored")
            return 1
        if getpass_fn("Repeat password: ") != pw:
            print("vault: passwords did not match; nothing stored")
            return 1
        v.put(host, args.username, Secret(pw))
        del pw
        print(f"stored {args.username} @ {host} in the OS keychain")
        return 0
    except VaultError as e:
        print(f"vault: {e}")
        return 1

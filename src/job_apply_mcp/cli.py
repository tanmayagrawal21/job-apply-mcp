"""Command line: ``job-apply-mcp [serve] | init | doctor | version | grant | vault | run | report | feedback``
(ADR-0020, ADR-0045).

With no subcommand the server starts on stdio, exactly like 0.1, so existing MCP client configs keep working.
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

from . import __version__

COMMANDS = {"serve", "init", "doctor", "version", "approve", "deny", "pending", "resolve", "grant", "vault",
            "profile", "run", "report", "feedback"}


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="job-apply-mcp", description="MCP server for discovering jobs and applying.")
    p.add_argument("--version", action="version", version=f"job-apply-mcp {__version__}")
    sub = p.add_subparsers(dest="cmd")

    s = sub.add_parser("serve", help="run the MCP server (default; stdio)")
    s.add_argument("--transport", choices=["stdio", "http"], default="stdio")
    s.add_argument("--host", default="127.0.0.1", help="http only; non-loopback needs JOB_APPLY_MCP_TOKEN")
    s.add_argument("--port", type=int, default=8765, help="http only")

    i = sub.add_parser("init", help="create config.toml, profile.toml and controls.toml")
    i.add_argument("--yes", "-y", action="store_true", help="non-interactive: accept detected defaults")
    i.add_argument("--force", action="store_true", help="overwrite existing files (old ones saved as .bak)")
    i.add_argument("--config-dir", type=Path, help="target directory (default: platform config dir)")
    i.add_argument("--model", help="Ollama model tag for the small tier (overrides RAM-based default)")
    i.add_argument("--client-command", choices=["auto", "uvx", "python", "script"], default="auto",
                   help="how the printed client snippet launches the server")
    for k in ("first-name", "last-name", "email", "phone", "city", "region", "country", "postal-code"):
        i.add_argument(f"--{k}", help=f"profile contact {k.replace('-', ' ')}")

    d = sub.add_parser("doctor", help="check setup and print fixes")
    d.add_argument("--json", action="store_true")
    d.add_argument("--offline", action="store_true", help="skip network probes of model servers")

    sub.add_parser("version", help="print the version")
    r = sub.add_parser("run", help="apply from explicit links, emailed links and discovery (ADR-0045)")
    r.add_argument("--links", dest="links_file", help="file with one job URL per line (# comments allowed)")
    r.add_argument("--link", action="append", default=[], help="a job URL (repeatable)")
    r.add_argument("--source", action="append", choices=["links", "mail", "discovery"],
                   help="restrict sources (repeatable; default intake.sources)")
    r.add_argument("--max", dest="max_applications", type=int, help="max applications this run")
    r.add_argument("--dry-run", action="store_true", help="force a dry run (never makes the run less safe)")
    for k in ("query", "since"):   # sender, recipient and label are config-only (intake.mail.*)
        r.add_argument(f"--mail-{k}", help=f"emailed links: narrow by {k} (overrides intake.mail.{k})")
    r.add_argument("--query", help="discovery query (overrides intake.discovery.query)")
    rp = sub.add_parser("report", help="show a run report (default: the latest run)")
    rp.add_argument("run_id", nargs="?", default="")
    rp.add_argument("--category", default="")
    rp.add_argument("--limit", type=int, default=50)
    fb = sub.add_parser("feedback", help="give feedback on a run (applied at the start of the next run)")
    fb.add_argument("run_id", nargs="?", default="")
    fb.add_argument("feedback", nargs="?", default="",
                    help="free text, or a JSON object such as '{\"kind\":\"deny_company\"}'")
    fb.add_argument("--ref", help="the run item the feedback is about")
    fb.add_argument("--confirm", type=int, metavar="ID",
                    help="HUMAN ONLY (needs a TTY): confirm client-supplied feedback so the next run applies it")
    for name, h in (("approve", "HUMAN ONLY (needs a TTY): approve a pending submission"),
                    ("deny", "HUMAN ONLY (needs a TTY): deny a pending submission"),
                    ("pending", "list pending approvals"),
                    ("resolve", "HUMAN ONLY (needs a TTY): resolve a submission stuck in submitting/unknown")):
        sub.add_parser(name, help=h).add_argument("rest", nargs="*")
    sub.add_parser("grant", help="HUMAN ONLY (needs a TTY): time-limited grants for agents (--list, --revoke)"
                   ).add_argument("rest", nargs=argparse.REMAINDER)
    sub.add_parser("profile", help="build your profile: ask | list | answer (open gaps the tool collected)"
                   ).add_argument("rest", nargs=argparse.REMAINDER)
    sub.add_parser("vault", help="HUMAN ONLY (needs a TTY): ATS logins in the OS keychain (add, list, rm)"
                   ).add_argument("rest", nargs=argparse.REMAINDER)
    return p


def main(argv: list[str] | None = None) -> None:
    argv = list(sys.argv[1:] if argv is None else argv)
    if not argv or (argv[0] not in COMMANDS and argv[0] not in ("-h", "--help", "--version")):
        argv.insert(0, "serve")
    args = build_parser().parse_args(argv)

    if args.cmd == "version":
        print(__version__)
    elif args.cmd == "init":
        from .setup_wizard import CONTACT_KEYS, run_init

        contact = {k: getattr(args, k) for k in CONTACT_KEYS if getattr(args, k, None)}
        raise SystemExit(run_init(yes=args.yes, force=args.force, config_dir=args.config_dir, contact=contact,
                                  model=args.model, command_style=args.client_command))
    elif args.cmd in ("approve", "deny", "pending", "resolve"):
        from .apply.approvals import cli as approvals_cli
        from .config import load_settings

        st = load_settings()
        raise SystemExit(approvals_cli([args.cmd, *args.rest], st.paths.data_dir, st.apply.approval_ttl_s))
    elif args.cmd == "grant":
        from .config import load_settings
        from .grants.cli import cli as grant_cli

        raise SystemExit(grant_cli(args.rest, load_settings()))
    elif args.cmd in ("run", "report", "feedback"):
        raise SystemExit(_intake_cli(args))
    elif args.cmd == "profile":
        from .config import load_settings
        from .persona.cli import cli as profile_cli

        raise SystemExit(profile_cli(args.rest, load_settings()))
    elif args.cmd == "vault":
        from .accounts.cli import cli as vault_cli
        from .config import load_settings

        raise SystemExit(vault_cli(args.rest, load_settings()))
    elif args.cmd == "doctor":
        from .doctor import run_doctor

        raise SystemExit(run_doctor(as_json=args.json, network=not args.offline))
    else:
        from .server import mcp

        if args.transport == "http":
            from .httpserve import run_http

            run_http(mcp, args.host, args.port)
        else:
            mcp.run()


def _intake_cli(args: argparse.Namespace) -> int:
    import asyncio
    import json

    from .config import load_settings
    from .intake.runner import IntakeRunner
    from .services import Services

    async def go() -> dict:
        svc = Services(load_settings())
        try:
            r = IntakeRunner.from_services(svc)
            if args.cmd == "report":
                return await r.report(args.run_id, args.category, args.limit)
            if args.cmd == "feedback":
                # a terminal user is the owner; anything else (pipes, agents) stays client-origin (ADR-0045)
                tty = sys.stdin.isatty() and sys.stdout.isatty()
                if args.confirm is not None:
                    if not tty:
                        return {"error": "--confirm needs an interactive terminal (a human)"}
                    return await r.confirm_feedback(args.confirm, "human:cli")
                fb: str | dict = args.feedback
                if fb.lstrip().startswith("{"):
                    fb = json.loads(fb)
                return await r.give_feedback(args.run_id, args.ref, fb, origin="user" if tty else "client")
            mail = {k: getattr(args, f"mail_{k}") for k in ("query", "since")
                    if getattr(args, f"mail_{k}") is not None}
            return await r.run(links=args.link, links_file=args.links_file, sources=args.source,
                               max_applications=args.max_applications, dry_run=True if args.dry_run else None,
                               mail=mail or None, discovery={"query": args.query} if args.query else None)
        finally:
            await svc.aclose()

    out = asyncio.run(go())
    print(json.dumps(out, indent=2, default=str))
    return 1 if "error" in out else 0


def init_main() -> None:
    """Console script ``job-apply-mcp-init``: alias of ``job-apply-mcp init``."""
    main(["init", *sys.argv[1:]])


if __name__ == "__main__":
    main()

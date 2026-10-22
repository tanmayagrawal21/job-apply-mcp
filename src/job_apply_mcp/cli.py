"""Command line: ``job-apply-mcp [serve] | init | doctor | version`` (ADR-0020).

With no subcommand the server starts on stdio, exactly like 0.1, so existing MCP client configs keep working.
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

from . import __version__

COMMANDS = {"serve", "init", "doctor", "version", "approve", "deny", "pending", "resolve", "grant"}


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
    for name, h in (("approve", "HUMAN ONLY (needs a TTY): approve a pending submission"),
                    ("deny", "HUMAN ONLY (needs a TTY): deny a pending submission"),
                    ("pending", "list pending approvals"),
                    ("resolve", "HUMAN ONLY (needs a TTY): resolve a submission stuck in submitting/unknown")):
        sub.add_parser(name, help=h).add_argument("rest", nargs="*")
    sub.add_parser("grant", help="HUMAN ONLY (needs a TTY): time-limited grants for agents (--list, --revoke)"
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


def init_main() -> None:
    """Console script ``job-apply-mcp-init``: alias of ``job-apply-mcp init``."""
    main(["init", *sys.argv[1:]])


if __name__ == "__main__":
    main()

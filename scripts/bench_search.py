#!/usr/bin/env python3
"""Optional live benchmark (opt-in; hits public job-board APIs): boards/sec and time-to-first-page.

    uv run python scripts/bench_search.py --boards-file boards.txt [--limit-boards 100] [--legacy]

`boards.txt` has one `platform:board` per line (e.g. `greenhouse:examplecorp`). `--legacy` forces the old global
limits (4 per host, 0.25s spacing) for a before/after comparison. Uses a throwaway data dir (cold HTTP cache).
Prints page-2 latency too, which should be ~0 because follow-up pages read the session (ADR-0024).
"""
from __future__ import annotations

import argparse
import asyncio
import tempfile
import time
from pathlib import Path

from job_apply_mcp.config import Settings, load_settings


async def main() -> None:
    from job_apply_mcp.services import Services

    ap = argparse.ArgumentParser()
    ap.add_argument("--boards-file", required=True)
    ap.add_argument("--limit-boards", type=int, default=100)
    ap.add_argument("--query", default="engineer")
    ap.add_argument("--legacy", action="store_true", help="disable per-host overrides (old behaviour)")
    a = ap.parse_args()
    lines = Path(a.boards_file).read_text(encoding="utf-8").splitlines()
    boards = [ln.strip() for ln in lines if ln.strip() and not ln.startswith("#")][: a.limit_boards]
    data = load_settings().model_dump()
    data["paths"]["data_dir"] = tempfile.mkdtemp(prefix="jam-bench-")
    if a.legacy:
        data["concurrency"].update(hosts={}, global_limit=16)
    svc = Services(Settings.model_validate(data))
    t0 = time.monotonic()
    out = await svc.search(query=a.query, boards=boards, since_days=0, limit=25)
    first = time.monotonic() - t0
    t1 = time.monotonic()
    await svc.search(query=a.query, boards=boards, since_days=0, limit=25, cursor=out.get("next"))
    page2 = time.monotonic() - t1
    st = svc.search_status(out["session"])
    print(f"mode={'legacy' if a.legacy else 'per-host'} boards={len(boards)} ok={st['boards_done'] - st['boards_failed']} "
          f"failed={st['boards_failed']} rows={out['total']}")
    print(f"time_to_first_page={first:.1f}s boards_per_s={len(boards) / first:.2f} page2={page2 * 1000:.0f}ms")
    await svc.aclose()


if __name__ == "__main__":
    asyncio.run(main())

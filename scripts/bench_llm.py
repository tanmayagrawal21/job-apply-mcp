#!/usr/bin/env python3
"""Optional live benchmark: tokens/s and latency for the locally configured Ollama models.

    uv run python scripts/bench_llm.py [--runs 3] [--models qwen3:4b,qwen2.5:7b]

Reads the same config as the server. Needs a running Ollama; never calls cloud providers.
"""
from __future__ import annotations

import argparse
import asyncio
import statistics
import time

from job_apply_mcp.config import load_settings
from job_apply_mcp.llm.providers.ollama import OllamaProvider

PROMPT = "List five skills a data analyst needs, one per line, no commentary."


async def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--runs", type=int, default=3)
    ap.add_argument("--models", default="")
    a = ap.parse_args()
    cfg = load_settings().llm
    p = OllamaProvider(**{**cfg.providers.get("ollama", {}), "timeout_s": 180})
    if not await p.available():
        raise SystemExit("Ollama not reachable")
    tiers = {t.model: t for t in (*cfg.tiers.values(), *cfg.task_tiers.values()) if t.provider == "ollama"}
    models = [m for m in a.models.split(",") if m] or list(tiers)
    have = set(await p.list_models())
    print(f"{'model':28} {'cold_s':>7} {'warm_ms':>8} {'tok/s':>7} {'tokens':>7}")
    for m in models:
        if m not in have:
            print(f"{m:28} not pulled")
            continue
        t = tiers.get(m)
        kw = {"model": m, "max_tokens": t.max_tokens if t else 128, "num_ctx": t.num_ctx if t else None,
              "think": t.think if t else False}
        t0 = time.monotonic()
        await p.complete_ex(PROMPT, **kw)
        cold = time.monotonic() - t0
        lat, rates, toks = [], [], 0
        for _ in range(a.runs):
            t0 = time.monotonic()
            c = await p.complete_ex(PROMPT, **kw)
            dt = time.monotonic() - t0
            lat.append(dt * 1000)
            toks = c.tokens_out
            rates.append(c.tokens_out / (c.gen_ms / 1000) if c.gen_ms else c.tokens_out / dt)
        print(f"{m:28} {cold:7.1f} {statistics.median(lat):8.0f} {statistics.median(rates):7.1f} {toks:7d}")


if __name__ == "__main__":
    asyncio.run(main())

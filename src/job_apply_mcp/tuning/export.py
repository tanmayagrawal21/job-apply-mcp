"""Optional export of a promoted adapter to Ollama as a Modelfile with ADAPTER (ADR-0017)."""
from __future__ import annotations

import shutil
import subprocess
from pathlib import Path
from typing import Any

from ..config import TuneCfg


def export_ollama(adapter: Path, out_dir: Path, cfg: TuneCfg, run_id: str) -> dict[str, Any]:
    if not cfg.ollama_base:
        return {"status": "skipped", "reason": "tune.ollama_base not set"}
    mf = out_dir / "Modelfile"
    mf.write_text(f"FROM {cfg.ollama_base}\nADAPTER {adapter.resolve()}\n")
    out: dict[str, Any] = {"status": "written", "modelfile": str(mf), "name": f"jam-style-{run_id}",
                           "note": "Ollama needs a PEFT-format safetensors adapter on the SAME base architecture; "
                                   "MLX adapters may need conversion first."}
    if cfg.export_create:
        if not shutil.which("ollama"):
            return {**out, "status": "error", "reason": "ollama binary not found"}
        r = subprocess.run(["ollama", "create", out["name"], "-f", str(mf)], capture_output=True, text=True,
                           timeout=600, check=False)
        out["status"] = "created" if r.returncode == 0 else "error"
        if r.returncode:
            out["reason"] = (r.stderr or r.stdout)[-300:]
    return out

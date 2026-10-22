"""Trainer Protocol + the MLX (macOS / Apple silicon) backend (ADR-0017). Other backends register by name."""
from __future__ import annotations

import os
import platform
import signal
import subprocess
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol, runtime_checkable

from ..config import TuneCfg
from ..core.registry import Registry


@dataclass
class TrainSpec:
    base_model: str
    data_dir: Path            # contains train.jsonl / valid.jsonl
    adapter_dir: Path         # adapters.safetensors is written here
    resume_adapter: Path | None = None   # continue on top of the promoted adapter
    cfg: TuneCfg | None = None


@dataclass
class TrainResult:
    adapter_dir: Path
    seconds: float
    iters_done: int | None = None
    timed_out: bool = False
    log_tail: str = ""


@runtime_checkable
class Trainer(Protocol):
    name: str

    def available(self) -> tuple[bool, str]:
        """(ok, reason). Must not import heavy libraries unless they are installed."""

    def train(self, spec: TrainSpec) -> TrainResult: ...

    def generate(self, base_model: str, adapter: Path | None, prompts: list[list[dict]],
                 max_tokens: int) -> list[str]:
        """Chat-format prompts (system+user messages) -> completions, with or without an adapter."""

    def nll(self, base_model: str, adapter: Path | None, pairs: list[tuple[list[dict], str]]) -> float:
        """Mean per-token negative log-likelihood of each reference completion given its prompt."""


trainers: Registry = Registry("tuning backend")


def has_adapter(d: Path) -> bool:
    return (d / "adapters.safetensors").is_file()


@trainers.register("mlx")
class MlxTrainer:
    name = "mlx"

    def available(self) -> tuple[bool, str]:
        if sys.platform != "darwin" or platform.machine() != "arm64":
            return False, "mlx backend needs macOS on Apple silicon"
        try:
            import importlib.util
            if importlib.util.find_spec("mlx_lm") is None:
                return False, "mlx-lm not installed: pip install 'job-apply-mcp[tune]'"
        except ImportError:  # pragma: no cover
            return False, "mlx-lm not importable"
        return True, "ok"

    def train(self, spec: TrainSpec) -> TrainResult:
        c = spec.cfg or TuneCfg()
        spec.adapter_dir.mkdir(parents=True, exist_ok=True)
        cmd = [sys.executable, "-m", "mlx_lm", "lora", "--model", spec.base_model, "--train",
               "--data", str(spec.data_dir), "--adapter-path", str(spec.adapter_dir),
               "--iters", str(c.iters), "--batch-size", str(c.batch_size), "--num-layers", str(c.num_layers),
               "--learning-rate", str(c.learning_rate), "--max-seq-length", str(c.max_seq_length),
               "--save-every", str(c.save_every), "--mask-prompt", "--steps-per-report", "10", "--steps-per-eval", "50"]
        if c.grad_checkpoint:
            cmd.append("--grad-checkpoint")
        if spec.resume_adapter and has_adapter(spec.resume_adapter):
            cmd += ["--resume-adapter-file", str(spec.resume_adapter / "adapters.safetensors")]
        log = spec.adapter_dir / "train.log"
        t0 = time.time()
        timed_out = False
        with log.open("w") as fh:
            p = subprocess.Popen(cmd, stdout=fh, stderr=subprocess.STDOUT, start_new_session=True)
            try:
                p.wait(timeout=c.time_budget_s)
            except subprocess.TimeoutExpired:
                timed_out = True
                os.killpg(p.pid, signal.SIGTERM)    # keep the latest periodic checkpoint
                try:
                    p.wait(timeout=30)
                except subprocess.TimeoutExpired:
                    os.killpg(p.pid, signal.SIGKILL)
        tail = "\n".join(log.read_text(errors="replace").splitlines()[-15:])
        if not has_adapter(spec.adapter_dir):
            raise RuntimeError(f"training produced no adapter (rc={p.returncode}):\n{tail}")
        if p.returncode not in (0, None) and not timed_out:
            raise RuntimeError(f"mlx_lm lora failed (rc={p.returncode}):\n{tail}")
        done = None
        for ln in reversed(tail.splitlines()):
            if ln.startswith("Iter "):
                try:
                    done = int(ln.split()[1].rstrip(":"))
                except ValueError:
                    pass
                break
        return TrainResult(spec.adapter_dir, time.time() - t0, done, timed_out, tail)

    # -- scoring helpers (lazy heavy imports) -------------------------------------------------
    @staticmethod
    def _load(base_model: str, adapter: Path | None):
        from mlx_lm import load
        return load(base_model, adapter_path=str(adapter) if adapter else None)

    @staticmethod
    def _prompt(tok, messages: list[dict]) -> str:
        try:
            return tok.apply_chat_template(messages, tokenize=False, add_generation_prompt=True,
                                           enable_thinking=False)
        except TypeError:
            return tok.apply_chat_template(messages, tokenize=False, add_generation_prompt=True)

    def generate(self, base_model, adapter, prompts, max_tokens):
        from mlx_lm import generate
        model, tok = self._load(base_model, adapter)
        return [generate(model, tok, prompt=self._prompt(tok, m), max_tokens=max_tokens, verbose=False).strip()
                for m in prompts]

    def nll(self, base_model, adapter, pairs):
        import mlx.core as mx
        from mlx import nn
        model, tok = self._load(base_model, adapter)
        tot, n = 0.0, 0
        for messages, ref in pairs:
            p_ids = tok.encode(self._prompt(tok, messages))
            r_ids = tok.encode(ref + (tok.eos_token or ""), add_special_tokens=False)
            ids = mx.array([p_ids + r_ids])
            logits = model(ids[:, :-1])
            tgt = ids[:, 1:]
            losses = nn.losses.cross_entropy(logits, tgt, reduction="none")[0, len(p_ids) - 1:]
            tot += float(mx.sum(losses).item())
            n += len(r_ids)
        return tot / max(1, n)

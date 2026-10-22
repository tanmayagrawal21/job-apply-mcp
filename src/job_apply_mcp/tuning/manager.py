"""Tuning orchestration: runs, promotion, rollback, new-data trigger (ADR-0017)."""
from __future__ import annotations

import json
import shutil
import threading
import time
from pathlib import Path
from typing import Any

from ..config import Settings
from . import guards
from .dataset import build_dataset
from .evalgate import evaluate
from .export import export_ollama
from .source import trusted_items
from .trainer import Trainer, TrainSpec, has_adapter, trainers

_lock = threading.Lock()


class TuneManager:
    def __init__(self, settings: Settings, source: Any, trainer: Trainer | None = None,
                 battery=guards.on_battery, resident=guards.resident_models):
        self.settings, self.cfg, self.source = settings, settings.tune, source
        self._trainer = trainer
        self._battery, self._resident = battery, resident
        self.root = Path(settings.paths.data_dir).expanduser() / "tuning"

    # ---------------------------------------------------------------- state
    @property
    def trainer(self) -> Trainer:
        if self._trainer is None:
            self._trainer = trainers.create(self.cfg.backend)
        return self._trainer

    def _state(self) -> dict[str, Any]:
        p = self.root / "state.json"
        base = {"promoted": None, "previous": None, "seen_ids": [], "runs": {}, "running": None}
        return {**base, **json.loads(p.read_text())} if p.is_file() else base

    def _save(self, st: dict[str, Any]) -> None:
        self.root.mkdir(parents=True, exist_ok=True)
        tmp = self.root / "state.json.tmp"
        tmp.write_text(json.dumps(st, indent=1))
        tmp.replace(self.root / "state.json")

    def adapter_dir(self, run_id: str | None) -> Path | None:
        return self.root / run_id / "adapter" if run_id else None

    def _eligible_ids(self) -> list[str]:
        c = self.cfg
        return sorted(r.id for r in trusted_items(self.source, None) if r.kind in c.kinds and r.kind != c.facts_kind)

    def new_texts(self, st: dict[str, Any] | None = None) -> int:
        seen = set((st or self._state())["seen_ids"])
        return sum(1 for i in self._eligible_ids() if i not in seen)

    # ---------------------------------------------------------------- tools' backing calls
    def status(self) -> dict[str, Any]:
        st = self._state()
        ok, why = self._backend_ok()
        last = max(st["runs"].items(), default=(None, None))[1]
        out: dict[str, Any] = {
            "backend": self.cfg.backend, "backend_ok": ok, "base": self.cfg.base_model,
            "promoted": st["promoted"], "previous": st["previous"], "running": st["running"],
            "new_texts": self.new_texts(st), "threshold": self.cfg.new_data_threshold,
            "runs": len(st["runs"]), "last": last,
        }
        if not ok:
            out["backend_note"] = why
        return out

    def _backend_ok(self) -> tuple[bool, str]:
        try:
            return self.trainer.available()
        except KeyError as e:
            return False, str(e)

    def rollback(self) -> dict[str, Any]:
        st = self._state()
        if not st["previous"] and not st["promoted"]:
            return {"status": "noop", "reason": "nothing promoted"}
        gone = st["promoted"]
        st["promoted"], st["previous"] = st["previous"], None
        self._save(st)
        return {"status": "rolled_back", "from": gone, "promoted": st["promoted"] or "base"}

    def run(self, trigger: str = "manual", force: bool = False) -> dict[str, Any]:
        if trigger not in ("manual", "new_data"):
            return {"status": "error", "reason": f"unknown trigger '{trigger}'"}
        if not _lock.acquire(blocking=False):
            return {"status": "skipped", "reason": "a tuning run is already in progress"}
        try:
            return self._run(trigger, force)
        finally:
            _lock.release()

    def _run(self, trigger: str, force: bool) -> dict[str, Any]:
        c = self.cfg
        st = self._state()
        n_new = self.new_texts(st)
        if trigger == "new_data" and n_new < c.new_data_threshold:
            return {"status": "skipped", "reason": f"{n_new} new texts < threshold {c.new_data_threshold}"}
        ok, why = self._backend_ok()
        if not ok:
            return {"status": "skipped", "reason": why}
        if not force and (why := guards.check(self.settings, self._battery, self._resident)):
            return {"status": "skipped", "reason": why}

        run_id = time.strftime("%Y%m%d-%H%M%S")
        run_dir = self.root / run_id
        ds = build_dataset(self.source, c, run_dir)
        if not ds.train:
            return {"status": "skipped", "reason": "no usable training examples", "dataset": ds.summary()}
        st["running"] = run_id
        self._save(st)
        rec: dict[str, Any] = {"id": run_id, "trigger": trigger, "base": c.base_model, "dataset": ds.summary(),
                               "started": time.strftime("%Y-%m-%dT%H:%M:%S")}
        try:
            incumbent = self.adapter_dir(st["promoted"])
            if incumbent and not has_adapter(incumbent):
                incumbent = None
            res = self.trainer.train(TrainSpec(c.base_model, run_dir, run_dir / "adapter", incumbent, c))
            rec["train"] = {"seconds": round(res.seconds), "iters": res.iters_done, "timed_out": res.timed_out}
            gate = evaluate(self.trainer, c, ds.eval, res.adapter_dir, incumbent)
            rec["gate"] = gate
            rec["decision"] = gate["decision"]
        except Exception as e:  # noqa: BLE001   # record the failure; never leave state "running"
            rec["decision"], rec["error"] = "error", str(e)[:500]
        st = self._state()
        st["running"] = None
        st["seen_ids"] = sorted(set(st["seen_ids"]) | set(ds.source_ids))
        if rec["decision"] == "promote":
            st["previous"], st["promoted"] = st["promoted"], run_id
            if c.ollama_base:
                rec["export"] = export_ollama(run_dir / "adapter", run_dir, c, run_id)
        st["runs"][run_id] = {k: rec[k] for k in ("trigger", "decision", "dataset", "train", "error")
                              if k in rec} | ({"nll_gain": rec["gate"].get("nll_gain"),
                                               "reasons": rec["gate"].get("reasons")} if "gate" in rec else {})
        self._save(st)
        (run_dir / "run.json").write_text(json.dumps(rec, indent=1, default=str))
        self._prune(st)
        return {"status": rec["decision"], "run": run_id, **{k: rec[k] for k in ("gate", "train", "error", "export")
                                                              if k in rec}}

    def _prune(self, st: dict[str, Any]) -> None:
        keep = {st["promoted"], st["previous"]}
        old = sorted(d for d in st["runs"] if d not in keep)
        for rid in old[: max(0, len(old) - self.cfg.keep_runs)]:
            shutil.rmtree(self.root / rid / "adapter", ignore_errors=True)

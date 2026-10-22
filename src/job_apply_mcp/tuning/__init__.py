"""Continuous local style tuning (ADR-0017). Heavy deps (mlx-lm) are imported lazily by the backend."""
from .dataset import Dataset, build_dataset
from .manager import TuneManager
from .trainer import Trainer, TrainResult, TrainSpec, trainers

__all__ = ["Dataset", "TrainResult", "TrainSpec", "Trainer", "TuneManager", "build_dataset", "trainers"]

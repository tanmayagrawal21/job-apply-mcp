"""Resource guards: never train on battery or beside another heavy resident model, unless configured."""
from __future__ import annotations

import subprocess
import sys

import httpx

from ..config import Settings


def on_battery() -> bool:
    if sys.platform != "darwin":
        return False
    try:
        out = subprocess.run(["pmset", "-g", "batt"], capture_output=True, text=True, timeout=5, check=False).stdout
    except (OSError, subprocess.SubprocessError):
        return False
    return "Battery Power" in out


def resident_models(settings: Settings) -> list[str]:
    """Heavy models (>= tune.heavy_model_gb) currently loaded in Ollama (empty if unreachable)."""
    base = settings.llm.providers.get("ollama", {}).get("base_url", "http://127.0.0.1:11434")
    try:
        r = httpx.get(f"{base}/api/ps", timeout=2.0)
        floor = settings.tune.heavy_model_gb * 2**30
        return [m.get("name", "?") for m in r.json().get("models", []) if m.get("size", 0) >= floor]
    except (httpx.HTTPError, ValueError):
        return []


def check(settings: Settings, battery=on_battery, resident=resident_models) -> str | None:
    """Return a human reason to refuse, or None when training may start."""
    t = settings.tune
    if not t.allow_on_battery and battery():
        return "on battery power (set tune.allow_on_battery=true to override)"
    if not t.allow_with_heavy_model:
        r = resident(settings)
        if r:
            return f"another model is resident ({', '.join(r[:3])}); unload it or set tune.allow_with_heavy_model=true"
    return None

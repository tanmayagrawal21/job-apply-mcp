"""Cross-platform default locations (stdlib only; ADR-0020).

This is the ONE resolver for every user-level location (config, data, cache, profile, privacy terms, consent and
vault metadata under data). ``$XDG_CONFIG_HOME`` / ``$XDG_DATA_HOME`` / ``$XDG_CACHE_HOME`` are honoured on every OS
when set to an absolute path (relative values are ignored, per the XDG spec: they would resolve against whatever the
cwd of each process happens to be). Otherwise POSIX uses ``~/.config``, ``~/.local/share`` and ``~/.cache``;
Windows uses ``%APPDATA%``, ``%LOCALAPPDATA%`` and ``%LOCALAPPDATA%\\<app>\\Cache``.
``JOB_APPLY_MCP_CONFIG`` (a file) still wins for the config file location (ADR-0054).
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

APP = "job-apply-mcp"


def _is_windows() -> bool:
    return sys.platform == "win32"


def _xdg(name: str) -> Path | None:
    v = os.environ.get(name, "")
    return Path(v) if v and Path(v).is_absolute() else None


def config_dir() -> Path:
    base = _xdg("XDG_CONFIG_HOME")
    if base is None:
        base = (Path(os.environ.get("APPDATA") or Path.home() / "AppData" / "Roaming") if _is_windows()
                else Path.home() / ".config")
    return base / APP


def data_dir() -> Path:
    base = _xdg("XDG_DATA_HOME")
    if base is None:
        base = (Path(os.environ.get("LOCALAPPDATA") or Path.home() / "AppData" / "Local") if _is_windows()
                else Path.home() / ".local" / "share")
    return base / APP


def cache_dir() -> Path:
    base = _xdg("XDG_CACHE_HOME")
    if base is not None:
        return base / APP
    if _is_windows():
        return Path(os.environ.get("LOCALAPPDATA") or Path.home() / "AppData" / "Local") / APP / "Cache"
    return Path.home() / ".cache" / APP


def default_config_file() -> Path:
    """The platform-default user config: the only file that may relax security settings (ADR-0021)."""
    return config_dir() / "config.toml"


def config_file() -> Path:
    env = os.environ.get("JOB_APPLY_MCP_CONFIG")
    return Path(env).expanduser() if env else default_config_file()


def profile_file() -> Path:
    return config_dir() / "profile.toml"


def controls_file() -> Path:
    return config_dir() / "controls.toml"


def privacy_terms_file() -> Path:
    return config_dir() / "privacy_terms.txt"


def privacy_allow_file() -> Path:
    return config_dir() / "privacy_allow.txt"


def default_config_dir_str() -> str:
    """String default for config models: '~' form on POSIX (portable in shared files), absolute on Windows."""
    if _is_windows() or _xdg("XDG_CONFIG_HOME"):
        return str(config_dir())
    return f"~/.config/{APP}"


def default_data_dir_str() -> str:
    if _is_windows() or _xdg("XDG_DATA_HOME"):
        return str(data_dir())
    return f"~/.local/share/{APP}"

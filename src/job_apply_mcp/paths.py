"""Cross-platform default locations (stdlib only; ADR-0020).

POSIX (Linux and macOS) keeps the XDG layout used since 0.1: ``~/.config/job-apply-mcp`` and
``~/.local/share/job-apply-mcp`` (``$XDG_CONFIG_HOME`` / ``$XDG_DATA_HOME`` are honoured). Windows uses
``%APPDATA%\\job-apply-mcp`` for config and ``%LOCALAPPDATA%\\job-apply-mcp`` for data.
``JOB_APPLY_MCP_CONFIG`` (a file) still wins for the config file location.
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

APP = "job-apply-mcp"


def _is_windows() -> bool:
    return sys.platform == "win32"


def config_dir() -> Path:
    if _is_windows():
        base = os.environ.get("APPDATA") or str(Path.home() / "AppData" / "Roaming")
        return Path(base) / APP
    xdg = os.environ.get("XDG_CONFIG_HOME")
    return (Path(xdg) if xdg else Path.home() / ".config") / APP


def data_dir() -> Path:
    if _is_windows():
        base = os.environ.get("LOCALAPPDATA") or str(Path.home() / "AppData" / "Local")
        return Path(base) / APP
    xdg = os.environ.get("XDG_DATA_HOME")
    return (Path(xdg) if xdg else Path.home() / ".local" / "share") / APP


def config_file() -> Path:
    env = os.environ.get("JOB_APPLY_MCP_CONFIG")
    return Path(env).expanduser() if env else config_dir() / "config.toml"


def profile_file() -> Path:
    return config_dir() / "profile.toml"


def controls_file() -> Path:
    return config_dir() / "controls.toml"


def default_config_dir_str() -> str:
    """String default for config models: '~' form on POSIX (portable in shared files), absolute on Windows."""
    if _is_windows() or os.environ.get("XDG_CONFIG_HOME"):
        return str(config_dir())
    return f"~/.config/{APP}"


def default_data_dir_str() -> str:
    if _is_windows() or os.environ.get("XDG_DATA_HOME"):
        return str(data_dir())
    return f"~/.local/share/{APP}"

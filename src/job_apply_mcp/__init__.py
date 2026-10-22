"""job-apply-mcp: modular, parallel, token-frugal MCP server for job discovery and applications."""
from __future__ import annotations

from importlib import metadata as _md

try:  # single source of truth: pyproject.toml [project].version, read from installed metadata
    __version__ = _md.version("job-apply-mcp")
except _md.PackageNotFoundError:  # running from a bare source tree
    __version__ = "0.0.0+unknown"

__all__ = ["__version__"]

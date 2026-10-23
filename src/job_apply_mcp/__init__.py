"""job-apply-mcp: modular, parallel, token-frugal MCP server for job discovery and applications."""
from __future__ import annotations

from importlib import metadata as _md


def _installed_version() -> str:
    # single source of truth: pyproject.toml [project].version, read from installed metadata.
    # The distribution is "applywise" (ADR-0042); "job-apply-mcp" covers installs made before the rename.
    for dist in ("applywise", "job-apply-mcp"):
        try:
            return _md.version(dist)
        except _md.PackageNotFoundError:
            continue
    return "0.0.0+unknown"  # running from a bare source tree


__version__ = _installed_version()

__all__ = ["__version__"]

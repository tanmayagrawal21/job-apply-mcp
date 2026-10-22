"""Platform adapters (ADR-0007). Importing this package registers every adapter in core.registry.platforms."""
from __future__ import annotations

import os
from collections.abc import Mapping

from ..config import PlatformsCfg
from ..core import registry
from ..core.interfaces import PlatformAdapter
from . import (  # noqa: F401  (self-register)
    ashby,
    greenhouse,
    jobspy_adapter,
    lever,
    smartrecruiters,
    workday,
)
from .base import AdapterError, NotSupported
from .http import HttpClient, HttpError

__all__ = ["AdapterError", "HttpClient", "HttpError", "NotSupported", "build_adapters", "detect"]


def build_adapters(names: list[str], http: HttpClient, cfg: PlatformsCfg | None = None,
                   env: Mapping[str, str] | None = None) -> dict[str, PlatformAdapter]:
    """Instantiate the enabled adapters that exist (names such as 'generic' that no component has registered
    yet are skipped), in the given order."""
    known = set(registry.platforms.names())
    return {n: registry.platforms.create(n, http=http, cfg=cfg, env=os.environ if env is None else env)
            for n in names if n in known}


def detect(adapters: Mapping[str, PlatformAdapter], url: str) -> PlatformAdapter | None:
    """First adapter whose matches(url) is true, in the order the adapters were enabled."""
    for a in adapters.values():
        if a.matches(url):
            return a
    return None

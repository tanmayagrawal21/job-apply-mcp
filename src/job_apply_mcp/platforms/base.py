"""Shared adapter plumbing: ref/URL parsing, API-key lookup, posting finishing, submit helpers."""
from __future__ import annotations

import os
import re
from collections.abc import Mapping
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

from ..config import PlatformsCfg
from ..core.models import ApplicationForm, FieldKind, FormField, Posting, SubmitResult
from ..core.secrets import safe_ref
from .http import HttpClient
from .textflags import extract_salary, flag_text


class AdapterError(Exception):
    """A platform could not satisfy the request (bad ref, 404, unsupported operation)."""


class NotSupported(AdapterError):
    pass


class InvalidRef(AdapterError):
    """The ref or URL itself is malformed or names no posting: an input error for the caller, never a tool_gap
    (ADR-0074). The message is sanitised here, so a pasted URL never echoes userinfo, a query string, a fragment
    or a known secret."""

    def __init__(self, message: str):
        from ..core.secrets import safe_text
        super().__init__(safe_text(message))


class PostingClosed(AdapterError):
    """The platform API says the posting is gone or closed (404 / unlisted / no posting). Reported as `skipped`
    (closed), never as an owner action (ADR-0056)."""


def iso_date(ms_or_str: Any) -> str | None:
    """Epoch milliseconds / ISO string -> 'YYYY-MM-DD' (None when unknown)."""
    if ms_or_str in (None, "", 0):
        return None
    if isinstance(ms_or_str, (int, float)):
        return datetime.fromtimestamp(ms_or_str / 1000, tz=UTC).strftime("%Y-%m-%d")
    return str(ms_or_str)[:10]


def not_before(posted_at: str | None, since: str | None) -> bool:
    """True when the posting is at least as new as `since` (unknown dates are kept)."""
    return not since or not posted_at or posted_at[:10] >= since[:10]


class BaseAdapter:
    name: str = ""
    hosts: tuple[str, ...] = ()

    def __init__(self, http: HttpClient, cfg: PlatformsCfg | None = None, env: Mapping[str, str] | None = None):
        self.http = http
        self.cfg = cfg or PlatformsCfg()
        self.env = os.environ if env is None else env

    # ---------------------------------------------------------------- identity
    def matches(self, url: str) -> bool:
        host = (urlparse(url).netloc or "").lower().split(":")[0]
        return any(host == h or host.endswith("." + h) for h in self.hosts)

    def ref(self, board: str, job_id: str) -> str:
        return f"{self.name}:{board}:{job_id}"

    def parse(self, url_or_ref: str) -> tuple[str, str | None]:
        """(board, job_id|None) from a ref or a URL on this platform."""
        if url_or_ref.startswith(self.name + ":") and not url_or_ref.startswith(("http:", "https:")):
            parts = url_or_ref.split(":", 2)
            if len(parts) == 3 and parts[1]:
                return parts[1], parts[2] or None
            raise InvalidRef(f"malformed ref '{safe_ref(url_or_ref)}' (expected {self.name}:<board>:<id>)")
        return self._parse_url(url_or_ref)

    def _parse_url(self, url: str) -> tuple[str, str | None]:  # pragma: no cover - overridden
        raise NotImplementedError

    def _need_job(self, url_or_ref: str) -> tuple[str, str]:
        board, job = self.parse(url_or_ref)
        if not job:
            raise InvalidRef(f"'{safe_ref(url_or_ref)}' names a board, not a posting")
        return board, job

    # ---------------------------------------------------------------- credentials
    def api_key(self, board: str) -> str | None:
        """Per-board API key from the environment: <prefix>_<PLATFORM>_<BOARD> (non-alphanumerics -> '_')."""
        slug = re.sub(r"[^A-Za-z0-9]+", "_", board).strip("_").upper()
        return self.env.get(f"{self.cfg.api_key_env_prefix}_{self.name.upper()}_{slug}") or None

    # ---------------------------------------------------------------- structured country (ADR-0040)
    @staticmethod
    def country_code(value: Any, *, allow_codes: bool = True) -> str | None:
        """A structured country field -> ISO-2 ("United States" -> "US", "Canada" -> "CA"); None if empty or unknown.
        `allow_codes=False` for free text, where a two-letter tail may be a state ("CA" = California)."""
        from ..apply.places import country_iso  # data only; no apply-engine import at module load
        return country_iso(value, allow_codes=allow_codes)

    # ---------------------------------------------------------------- posting assembly
    def finish(self, text: str, *, with_description: bool, api_salary: str | None = None, **p: Any) -> Posting:
        """Build a Posting: flags and salary always come from `text`; the description only on request."""
        flags = flag_text(text) if text else {}
        sal = api_salary
        if not sal and text and (r := extract_salary(text)):
            sal = r.summary()
        if isinstance(p.get("title"), str):
            p["title"] = p["title"].strip()  # Ashby/SmartRecruiters titles carry stray whitespace
        if isinstance(p.get("location"), str):
            p["location"] = re.sub(r"(,\s*)+", ", ", p["location"]).strip(" ,")  # 'Dublin, , Ireland'
        desc = text[: self.cfg.max_description_chars] if with_description and text else None
        return Posting(platform=self.name, flags=flags, salary=sal, description=desc, **p)

    # ---------------------------------------------------------------- forms & submit
    @staticmethod
    def missing_required(form: ApplicationForm, answers: dict[str, Any], files: dict[str, str]) -> list[str]:
        return [f.key for f in form.fields if f.required
                and (files.get(f.key) is None if f.kind == FieldKind.file else answers.get(f.key) in (None, ""))]

    @staticmethod
    def read_files(files: dict[str, str]) -> dict[str, tuple[str, bytes]]:
        out = {}
        for k, path in files.items():
            p = Path(path).expanduser()
            out[k] = (p.name, p.read_bytes())
        return out

    def needs_browser(self, form: ApplicationForm, why: str) -> SubmitResult:
        return SubmitResult(ref=form.ref, status="needs_browser", via=None, detail=why)

    def std_fields(self, *specs: tuple[str, str, FieldKind, bool]) -> list[FormField]:
        return [FormField(key=k, label=l, kind=kd, required=r) for k, l, kd, r in specs]

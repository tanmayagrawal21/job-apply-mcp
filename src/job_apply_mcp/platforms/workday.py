"""Workday: the public CXS JSON endpoints behind every *.myworkdayjobs.com career site.

Board id: '<tenant>.<wdN>/<site>' (e.g. 'acme.wd5/External'). Job id: the path after '/job/'.
Applying requires a candidate account, which this project never creates, so submit() is always needs_browser.
"""
from __future__ import annotations

import re
from collections.abc import AsyncIterator
from datetime import UTC, date, datetime, timedelta
from typing import Any
from urllib.parse import urlparse

from ..core.models import ApplicationForm, Posting, SubmitResult
from ..core.registry import platforms
from .base import AdapterError, BaseAdapter, InvalidRef, not_before
from .http import HttpError
from .textflags import html_to_text

_PAGE = 20  # Workday rejects larger pages


def _posted(s: str | None, today: date | None = None) -> str | None:
    """'Posted 3 Days Ago' / 'Posted Today' / 'Posted 30+ Days Ago' -> ISO date (approximate for 30+)."""
    if not s:
        return None
    today = today or datetime.now(UTC).date()
    t = s.lower()
    if "today" in t:
        return today.isoformat()
    if "yesterday" in t:
        return (today - timedelta(days=1)).isoformat()
    if m := re.search(r"(\d+)\+?\s*days?", t):
        return (today - timedelta(days=int(m.group(1)))).isoformat()
    return None


@platforms.register("workday")
class WorkdayAdapter(BaseAdapter):
    name = "workday"
    hosts = ("myworkdayjobs.com",)

    @staticmethod
    def _host(board: str) -> str:
        return board.split("/")[0] + ".myworkdayjobs.com"

    def _api(self, board: str) -> str:
        hostpart, site = board.split("/", 1)
        return f"https://{self._host(board)}/wday/cxs/{hostpart.split('.')[0]}/{site}"

    def _parse_url(self, url: str) -> tuple[str, str | None]:
        p = urlparse(url)
        labels = p.netloc.split(".")
        if len(labels) < 4 or "myworkdayjobs" not in labels:
            raise InvalidRef(f"not a Workday URL: {url}")
        parts = [x for x in p.path.split("/") if x]
        if parts and re.fullmatch(r"[a-z]{2}-[A-Z]{2}", parts[0]):
            parts = parts[1:]
        if parts and parts[0] == "wday":  # .../wday/cxs/<tenant>/<site>/job/...
            parts = parts[3:]
        if not parts:
            raise InvalidRef(f"no Workday site in URL: {url}")
        board = f"{labels[0]}.{labels[1]}/{parts[0]}"
        if "job" in parts:
            tail = [x for x in parts[parts.index("job") + 1:] if x != "apply"]
            return board, "/".join(tail) or None
        return board, None

    def _to_posting(self, board: str, d: dict, *, with_description: bool, ext: str | None = None) -> Posting:
        info = d.get("jobPostingInfo", d)
        path = (ext or info.get("externalPath") or "").removeprefix("/job/").strip("/")
        site = board.split("/", 1)[1]
        url = info.get("externalUrl") or f"https://{self._host(board)}/{site}/job/{path}"
        org = (d.get("hiringOrganization") or {}).get("name") or board.split(".")[0]
        text = html_to_text(info.get("jobDescription"))
        posted = info.get("startDate") or _posted(info.get("postedOn"))
        return self.finish(
            text, with_description=with_description,
            ref=self.ref(board, path), company=org, title=info.get("title", ""),
                 location=info.get("location") or info.get("locationsText") or "", url=url,
                 apply_url=url.rstrip("/") + ("" if url.endswith("/apply") else "/apply"),
                 posted_at=posted)

    async def list_postings(self, board: str, *, since: str | None = None, query: str = "",
                            with_description: bool = False) -> AsyncIterator[Posting]:
        api, total = self._api(board), None
        for page in range(self.cfg.workday_max_pages):
            body = {"appliedFacets": {}, "limit": _PAGE, "offset": page * _PAGE, "searchText": query}
            try:
                d = await self.http.post_json(f"{api}/jobs", json_body=body)
            except HttpError as e:
                raise AdapterError("site not found" if e.status in (400, 404) else str(e)) from e
            if total is None:
                total = int(d.get("total", 0))  # only the first page reports it
            rows = d.get("jobPostings") or []
            for r in rows:
                p = self._to_posting(board, r, with_description=False)
                if not_before(p.posted_at, since):
                    yield p
            if not rows or (page + 1) * _PAGE >= total:
                break

    async def get_posting(self, url_or_ref: str, *, with_description: bool = False) -> Posting:
        board, path = self._need_job(url_or_ref)
        try:
            d = await self.http.get_json(f"{self._api(board)}/job/{path}")
        except HttpError as e:
            raise AdapterError("job not found (closed or moved)" if e.status in (400, 404) else str(e)) from e
        return self._to_posting(board, d, with_description=with_description, ext=path)

    async def get_form(self, url_or_ref: str) -> ApplicationForm:
        """Questions appear only after candidate sign-in, so the schema is empty and the browser is the path."""
        board, path = self._need_job(url_or_ref)
        return ApplicationForm(ref=self.ref(board, path), fields=[], submit_via=["browser"])

    async def submit(self, form: ApplicationForm, answers: dict[str, Any], files: dict[str, str],
                     *, dry_run: bool = True) -> SubmitResult:
        return self.needs_browser(form, "Workday requires a candidate account; sign-in is a human step")

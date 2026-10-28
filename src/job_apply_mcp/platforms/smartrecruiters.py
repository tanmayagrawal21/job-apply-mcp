"""SmartRecruiters: public Posting API (api.smartrecruiters.com/v1). Submission needs the browser."""
from __future__ import annotations

import re
from collections.abc import AsyncIterator
from typing import Any

from ..core.models import ApplicationForm, FieldKind, Posting, SubmitResult
from ..core.registry import platforms
from .base import AdapterError, BaseAdapter, InvalidRef, iso_date, not_before
from .http import HttpError
from .textflags import html_to_text

API = "https://api.smartrecruiters.com/v1/companies"
_URL = re.compile(r"smartrecruiters\.com/(?:v1/companies/|oneclick-ui/company/)?([^/?#]+)(?:/(?:postings/)?(\d+))?", re.IGNORECASE)
_PAGE = 100


@platforms.register("smartrecruiters")
class SmartRecruitersAdapter(BaseAdapter):
    name = "smartrecruiters"
    hosts = ("smartrecruiters.com",)

    def _parse_url(self, url: str) -> tuple[str, str | None]:
        m = _URL.search(url)
        if not m:
            raise InvalidRef(f"not a SmartRecruiters URL: {url}")
        return m.group(1), m.group(2)

    @staticmethod
    def _loc(d: dict) -> str:
        loc = d.get("location") or {}
        return loc.get("fullLocation") or ", ".join(x for x in (loc.get("city"), loc.get("region"), loc.get("country")) if x)

    def _to_posting(self, board: str, d: dict, *, with_description: bool) -> Posting:
        sections = (d.get("jobAd") or {}).get("sections") or {}
        text = " ".join(html_to_text(s.get("text")) for s in sections.values() if isinstance(s, dict))
        pid = str(d["id"])
        url = d.get("postingUrl") or f"https://jobs.smartrecruiters.com/{board}/{pid}"
        return self.finish(
            text, with_description=with_description,
            ref=self.ref(board, pid), company=(d.get("company") or {}).get("name") or board,
                 title=d.get("name", ""), location=self._loc(d), url=url, apply_url=d.get("applyUrl") or url,
                 posted_at=iso_date(d.get("releasedDate")))

    async def list_postings(self, board: str, *, since: str | None = None, query: str = "",
                            with_description: bool = False) -> AsyncIterator[Posting]:
        offset, total = 0, None
        while total is None or offset < total:
            try:
                d = await self.http.get_json(f"{API}/{board}/postings", params={"limit": _PAGE, "offset": offset})
            except HttpError as e:
                raise AdapterError("board not found" if e.status == 404 else str(e)) from e
            total = int(d.get("totalFound", 0))
            rows = d.get("content") or []
            for r in rows:
                p = self._to_posting(board, r, with_description=with_description)
                if not_before(p.posted_at, since):
                    yield p
            if not rows:
                break
            offset += len(rows)

    async def get_posting(self, url_or_ref: str, *, with_description: bool = False) -> Posting:
        board, pid = self._need_job(url_or_ref)
        try:
            d = await self.http.get_json(f"{API}/{board}/postings/{pid}")
        except HttpError as e:
            raise AdapterError("job not found (closed or moved)" if e.status == 404 else str(e)) from e
        return self._to_posting(board, d, with_description=with_description)

    async def get_form(self, url_or_ref: str) -> ApplicationForm:
        """No public schema: returns the standard fields; screening questions need the browser."""
        board, pid = self._need_job(url_or_ref)
        F = FieldKind
        return ApplicationForm(ref=self.ref(board, pid), submit_via=["browser"], fields=self.std_fields(
            ("firstName", "First name", F.text, True), ("lastName", "Last name", F.text, True),
            ("email", "Email", F.text, True), ("phoneNumber", "Phone", F.text, False),
            ("location", "Location", F.location, False), ("resume", "Resume", F.file, True)))

    async def submit(self, form: ApplicationForm, answers: dict[str, Any], files: dict[str, str],
                     *, dry_run: bool = True) -> SubmitResult:
        return self.needs_browser(form, "SmartRecruiters application API requires a partner key; use the browser")

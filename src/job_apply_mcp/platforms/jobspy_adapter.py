"""Optional discovery-only adapter for LinkedIn / Indeed / Glassdoor via python-jobspy (extra: 'jobspy').

`board` is a site name (or comma list); `query`/`location` are the search. It never fetches forms or submits:
applying on those sites involves accounts and bot detection, which are human steps.
"""
from __future__ import annotations

import asyncio
import math
from collections.abc import AsyncIterator
from typing import Any

from ..core.models import ApplicationForm, Posting, SubmitResult
from ..core.registry import platforms
from .base import AdapterError, BaseAdapter, NotSupported, not_before

SITES = ("linkedin", "indeed", "glassdoor", "zip_recruiter", "google")


def _clean(v: Any) -> Any:
    return None if v is None or (isinstance(v, float) and math.isnan(v)) else v


@platforms.register("jobspy")
class JobSpyAdapter(BaseAdapter):
    name = "jobspy"
    hosts = ("linkedin.com", "indeed.com", "glassdoor.com")

    def _parse_url(self, url: str) -> tuple[str, str | None]:
        raise NotSupported("jobspy is discovery-only: use the apply_url of a search result with another adapter")

    @staticmethod
    def _scrape(**kw: Any) -> Any:
        try:
            from jobspy import scrape_jobs  # lazy: optional dependency
        except ImportError as e:
            raise AdapterError("python-jobspy is not installed; run: uv sync --extra jobspy") from e
        return scrape_jobs(**kw)

    async def list_postings(self, board: str, *, since: str | None = None, query: str = "", location: str = "",
                            with_description: bool = False) -> AsyncIterator[Posting]:
        sites = [s.strip() for s in board.split(",") if s.strip()] or self.cfg.jobspy_sites
        bad = [s for s in sites if s not in SITES]
        if bad:
            raise AdapterError(f"unknown jobspy site(s) {bad}; known: {list(SITES)}")
        df = await asyncio.to_thread(self._scrape, site_name=sites, search_term=query or None, location=location or None,
                                     results_wanted=self.cfg.jobspy_results, description_format="markdown")
        for r in df.to_dict("records"):
            r = {k: _clean(v) for k, v in r.items()}
            lo, hi = r.get("min_amount"), r.get("max_amount")
            pay = f"${lo:,.0f}-${hi:,.0f}" if lo and hi and (r.get("currency") or "USD") == "USD" else None
            posted = str(r["date_posted"])[:10] if r.get("date_posted") else None
            if not not_before(posted, since):
                continue
            site = r.get("site") or sites[0]
            yield self.finish(
            r.get("description") or "", with_description=with_description, api_salary=pay,
            ref=self.ref(site, str(r.get("id") or r.get("job_url"))), company=r.get("company") or "",
                     title=r.get("title") or "", location=r.get("location") or "", url=r.get("job_url") or "",
                     apply_url=r.get("job_url_direct") or r.get("job_url"), posted_at=posted)

    async def get_posting(self, url_or_ref: str, *, with_description: bool = False) -> Posting:
        raise NotSupported("jobspy is discovery-only; re-run the search for details")

    async def get_form(self, url_or_ref: str) -> ApplicationForm:
        raise NotSupported("jobspy is discovery-only")

    async def submit(self, form: ApplicationForm, answers: dict[str, Any], files: dict[str, str],
                     *, dry_run: bool = True) -> SubmitResult:
        return SubmitResult(ref=form.ref, status="needs_human", detail="discovery-only platform; apply manually")

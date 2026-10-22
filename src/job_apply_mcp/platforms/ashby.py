"""Ashby: public posting API for discovery; application form via the job board's non-user GraphQL query.
There is no public submission API, so submit() always returns needs_browser."""
from __future__ import annotations

import re
from collections.abc import AsyncIterator
from typing import Any

from ..core.models import ApplicationForm, FieldKind, FormField, Posting, SubmitResult
from ..core.registry import platforms
from .base import AdapterError, BaseAdapter, iso_date, not_before
from .http import HttpError

BOARD_API = "https://api.ashbyhq.com/posting-api/job-board"
GRAPHQL = "https://jobs.ashbyhq.com/api/non-user-graphql"
_URL = re.compile(r"ashbyhq\.com/([^/?#]+)(?:/([0-9a-f]{8}-[0-9a-f-]{27}))?", re.IGNORECASE)
_QUERY = ("query ApiJobPosting($organizationHostedJobsPageName: String!, $jobPostingId: String!) { "
          "jobPosting(organizationHostedJobsPageName: $organizationHostedJobsPageName, jobPostingId: $jobPostingId) "
          "{ applicationForm { sections { fieldEntries { ... on FormFieldEntry { isRequired field } } } } } }")
_KINDS = {"string": FieldKind.text, "email": FieldKind.text, "phone": FieldKind.text, "longtext": FieldKind.textarea,
          "file": FieldKind.file, "boolean": FieldKind.boolean, "valueselect": FieldKind.select,
          "multivalueselect": FieldKind.multiselect, "number": FieldKind.number, "date": FieldKind.date,
          "location": FieldKind.location, "sociallink": FieldKind.text}


@platforms.register("ashby")
class AshbyAdapter(BaseAdapter):
    name = "ashby"
    hosts = ("ashbyhq.com",)

    def _parse_url(self, url: str) -> tuple[str, str | None]:
        m = _URL.search(url)
        if not m or m.group(1) in ("posting-api", "api"):
            m = re.search(r"job-board/([^/?#]+)", url)
            if not m:
                raise AdapterError(f"not an Ashby URL: {url}")
            return m.group(1), None
        return m.group(1), m.group(2)

    def _to_posting(self, board: str, j: dict, *, with_description: bool) -> Posting:
        comp = j.get("compensation") or {}
        pay = comp.get("scrapeableCompensationSalarySummary") or comp.get("compensationTierSummary")
        url = j.get("jobUrl") or f"https://jobs.ashbyhq.com/{board}/{j['id']}"
        return self.finish(
            j.get("descriptionPlain") or "", with_description=with_description, api_salary=pay,
            ref=self.ref(board, j["id"]), company=board, title=j.get("title", ""), location=j.get("location") or "",
                 url=url, apply_url=j.get("applyUrl") or url + "/application",
                 posted_at=iso_date(j.get("publishedAt")), countries=self._countries(j))

    def _countries(self, j: dict) -> list[str]:
        """addressCountry of the primary and every secondary location (ADR-0040); any missing -> unknown ([])."""
        addrs = [j.get("address")] + [s.get("address") for s in j.get("secondaryLocations") or [] if isinstance(s, dict)]
        codes = [self.country_code(((a or {}).get("postalAddress") or {}).get("addressCountry")) for a in addrs]
        return sorted(set(codes)) if codes and all(codes) else []  # type: ignore[arg-type]

    async def _board(self, board: str) -> list[dict]:
        try:
            d = await self.http.get_json(f"{BOARD_API}/{board}", params={"includeCompensation": "true"})
        except HttpError as e:
            raise AdapterError("board not found" if e.status == 404 else str(e)) from e
        return [j for j in d.get("jobs", []) if j.get("isListed", True)]

    async def list_postings(self, board: str, *, since: str | None = None, query: str = "",
                            with_description: bool = False) -> AsyncIterator[Posting]:
        for j in await self._board(board):
            p = self._to_posting(board, j, with_description=with_description)
            if not_before(p.posted_at, since):
                yield p

    async def get_posting(self, url_or_ref: str, *, with_description: bool = False) -> Posting:
        board, jid = self._need_job(url_or_ref)
        for j in await self._board(board):
            if j.get("id") == jid:
                return self._to_posting(board, j, with_description=with_description)
        raise AdapterError("job not found (closed or unlisted)")

    async def get_form(self, url_or_ref: str) -> ApplicationForm:
        board, jid = self._need_job(url_or_ref)
        body = {"operationName": "ApiJobPosting", "query": _QUERY,
                "variables": {"organizationHostedJobsPageName": board, "jobPostingId": jid}}
        try:
            d = await self.http.post_json(GRAPHQL, params={"op": "ApiJobPosting"}, json_body=body,
                                          headers={"content-type": "application/json"})
        except HttpError as e:
            raise AdapterError(str(e)) from e
        if not isinstance(d, dict) or d.get("errors") or not (d.get("data") or {}).get("jobPosting"):
            raise AdapterError("Ashby form query returned no posting (closed, or the query shape changed)")
        fields: list[FormField] = []
        for sec in d["data"]["jobPosting"]["applicationForm"]["sections"]:
            for e in sec.get("fieldEntries") or []:
                f = e.get("field") or {}
                if not f:
                    continue
                opts = [str(v.get("label", v)) if isinstance(v, dict) else str(v) for v in f.get("selectableValues") or []]
                fields.append(FormField(key=str(f.get("path") or f.get("title")), label=str(f.get("title", "")),
                                        kind=_KINDS.get(str(f.get("type", "")).lower(), FieldKind.unknown),
                                        required=bool(e.get("isRequired")), options=opts))
        return ApplicationForm(ref=self.ref(board, jid), fields=fields, submit_via=["browser"])

    async def submit(self, form: ApplicationForm, answers: dict[str, Any], files: dict[str, str],
                     *, dry_run: bool = True) -> SubmitResult:
        return self.needs_browser(form, "Ashby has no public application API")
